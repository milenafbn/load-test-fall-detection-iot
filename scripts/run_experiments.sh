#!/usr/bin/env bash
# run_experiments.sh
# ------------------
# Orquestra os experimentos de sistemas distribuídos.
#
# Experimentos disponíveis:
#   A  → Escalabilidade horizontal: 1 nó vs 3 nós (mesmo total de devices)
#   B  → Tolerância a falhas: matar sensor-node-2 durante o teste
#   C  → Local vs Docker: compara baseline asyncio com cluster Docker
#   all→ Executa A, B e C em sequência
#
# Uso:
#   bash scripts/run_experiments.sh A
#   bash scripts/run_experiments.sh B
#   bash scripts/run_experiments.sh C
#   bash scripts/run_experiments.sh all
#
# Parâmetros configuráveis (variáveis de ambiente):
#   REQUESTS      → Requisições por device  (padrão: 100)
#   INTERVAL_MS   → Intervalo entre req em ms (padrão: 100)
#   DEVICES_TOTAL → Total de devices no pool  (padrão: 1000)
#   TC_LATENCY_MS → Latência artificial por nó sensor via tc/netem (padrão: 0)
#   TC_JITTER_MS  → Variação da latência via tc/netem             (padrão: 0)
#   TC_LOSS_PCT   → Perda de pacotes em porcentagem               (padrão: 0)
#   TC_RATE       → Limite de banda, ex: 1mbit ou 500kbit          (padrão: vazio)
#
# Exemplo com parâmetros customizados:
#   REQUESTS=50 INTERVAL_MS=200 bash scripts/run_experiments.sh A
#   TC_LATENCY_MS=100 TC_JITTER_MS=20 REQUESTS=50 bash scripts/run_experiments.sh A

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuração dos experimentos
# ---------------------------------------------------------------------------
REQUESTS="${REQUESTS:-100}"
INTERVAL_MS="${INTERVAL_MS:-100}"
DEVICES_TOTAL="${DEVICES_TOTAL:-1000}"
TC_ENABLED="${TC_ENABLED:-auto}"
TC_IFACE="${TC_IFACE:-eth0}"
TC_LATENCY_MS="${TC_LATENCY_MS:-0}"
TC_JITTER_MS="${TC_JITTER_MS:-0}"
TC_LOSS_PCT="${TC_LOSS_PCT:-0}"
TC_RATE="${TC_RATE:-}"
TC_STRICT="${TC_STRICT:-true}"

export TC_ENABLED TC_IFACE TC_LATENCY_MS TC_JITTER_MS TC_LOSS_PCT TC_RATE TC_STRICT

# Particionamento para 3 nós
NODE1_COUNT=334                          # devices 0–333
NODE2_COUNT=333                          # devices 334–666
NODE3_COUNT=333                          # devices 667–999
NODE2_OFFSET=334
NODE3_OFFSET=667

RESULTS_DIR="results"
SCRIPTS_DIR="scripts"

# ---------------------------------------------------------------------------
# Utilitários de terminal
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

banner()  { echo -e "\n${CYAN}${BOLD}══════════════════════════════════════════${RESET}"; \
            echo -e "${CYAN}${BOLD}  $*${RESET}"; \
            echo -e "${CYAN}${BOLD}══════════════════════════════════════════${RESET}\n"; }
info()    { echo -e "${GREEN}▶ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠  $*${RESET}"; }
error()   { echo -e "${RED}✗  $*${RESET}"; exit 1; }
step()    { echo -e "\n${BOLD}── $* ${RESET}"; }

# ---------------------------------------------------------------------------
# Pré-condições
# ---------------------------------------------------------------------------
check_prereqs() {
    command -v docker  &>/dev/null || error "Docker não encontrado."
    command -v python3 &>/dev/null || error "Python3 não encontrado."

    if ! docker compose ps --services 2>/dev/null | grep -q thingsboard; then
        warn "ThingsBoard não parece estar rodando."
        warn "Suba primeiro com: docker compose up thingsboard postgres -d"
        read -rp "Continuar mesmo assim? [s/N] " ans
        [[ "$ans" =~ ^[sS]$ ]] || exit 0
    fi

    [[ -f "device_tokens.json" ]] || error "device_tokens.json não encontrado. Execute provision_devices.py primeiro."
    mkdir -p "$RESULTS_DIR"

    if [[ "$TC_LATENCY_MS" != "0" || "$TC_JITTER_MS" != "0" || "$TC_LOSS_PCT" != "0" || -n "$TC_RATE" ]]; then
        info "Traffic Control: latency=${TC_LATENCY_MS}ms jitter=${TC_JITTER_MS}ms loss=${TC_LOSS_PCT}% rate=${TC_RATE:-sem limite}"
    fi
}

build_image() {
    step "Build da imagem sensor (usa cache se nada mudou)"
    docker compose build sensor-node-1
    info "Imagem pronta."
}

# ---------------------------------------------------------------------------
# Espera pelos arquivos de resultado de um nó
# wait_for_result <NODE_ID> <timeout_seconds>
# ---------------------------------------------------------------------------
wait_for_result() {
    local node_id="$1"
    local timeout="${2:-300}"
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        if ls "$RESULTS_DIR"/load_test_"${node_id}"_*.json &>/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    warn "Timeout aguardando resultado de $node_id"
    return 1
}

# ---------------------------------------------------------------------------
# EXPERIMENTO A — Escalabilidade horizontal
# ---------------------------------------------------------------------------
exp_a() {
    banner "EXPERIMENTO A — Escalabilidade Horizontal"
    info "Parâmetros: REQUESTS=$REQUESTS  INTERVAL_MS=$INTERVAL_MS  DEVICES=$DEVICES_TOTAL"
    echo ""
    echo "  Objetivo: comparar throughput e latência entre:"
    echo "    A1 → 1 nó Docker com $DEVICES_TOTAL devices"
    echo "    A3 → 3 nós Docker com ${NODE1_COUNT}+${NODE2_COUNT}+${NODE3_COUNT} devices cada"
    echo ""

    build_image

    # ── A1: Um único nó com todos os devices ────────────────────────────────
    step "A1 — Rodando 1 nó com $DEVICES_TOTAL devices..."
    info "Aguarde o teste completar (~$((REQUESTS * INTERVAL_MS / 1000))s por device)"

    EXP_REQUESTS=$REQUESTS EXP_INTERVAL_MS=$INTERVAL_MS \
    docker compose run --rm \
        -e NODE_ID=exp-a1-single \
        -e DEVICE_OFFSET=0 \
        -e DEVICE_COUNT="$DEVICES_TOTAL" \
        -e REQUESTS="$REQUESTS" \
        -e INTERVAL_MS="$INTERVAL_MS" \
        sensor-node-1

    A1_FILE=$(ls -t "$RESULTS_DIR"/load_test_exp-a1-single_*.json 2>/dev/null | head -1)
    [[ -n "$A1_FILE" ]] && info "A1 concluído → $A1_FILE" || warn "Resultado A1 não encontrado."

    # ── A3: Três nós em paralelo ─────────────────────────────────────────────
    step "A3 — Rodando 3 nós em paralelo..."
    info "Cada nó processa ~333 devices. Os 3 rodam simultaneamente."

    EXP_REQUESTS=$REQUESTS EXP_INTERVAL_MS=$INTERVAL_MS \
    docker compose --profile sensors up \
        --force-recreate \
        sensor-node-1 sensor-node-2 sensor-node-3

    N1=$(ls -t "$RESULTS_DIR"/load_test_node-1_*.json 2>/dev/null | head -1)
    N2=$(ls -t "$RESULTS_DIR"/load_test_node-2_*.json 2>/dev/null | head -1)
    N3=$(ls -t "$RESULTS_DIR"/load_test_node-3_*.json 2>/dev/null | head -1)

    if [[ -z "$N1" || -z "$N2" || -z "$N3" ]]; then
        warn "Alguns resultados A3 não foram encontrados. Verifique os containers."
    else
        info "A3 concluído → $N1, $N2, $N3"
    fi

    # ── Comparação A1 vs A3 ──────────────────────────────────────────────────
    step "Comparando resultados..."

    echo ""
    echo -e "${BOLD}── A3: métricas por nó e total do cluster ──${RESET}"
    [[ -n "$N1" && -n "$N2" && -n "$N3" ]] && \
        python3 "$SCRIPTS_DIR/compare_results.py" "$N1" "$N2" "$N3" --save

    if [[ -n "$A1_FILE" && -n "$N1" ]]; then
        echo ""
        echo -e "${BOLD}── A1 (1 nó) como baseline vs A3 (3 nós) ──${RESET}"
        CLUSTER_FILE=$(ls -t "$RESULTS_DIR"/cluster_*.json 2>/dev/null | head -1)
        # Extrai o JSON do cluster do arquivo composto para comparação direta
        python3 "$SCRIPTS_DIR/compare_results.py" "$N1" "$N2" "$N3" --baseline "$A1_FILE"
    fi

    banner "Experimento A concluído"
    echo "  Interpretação:"
    echo "    • Throughput cluster > throughput 1 nó → escalonamento funciona"
    echo "    • Latência similar    → distribuição não adiciona overhead significativo"
    echo "    • Throughput cluster ≈ N × throughput 1 nó → escalonamento linear"
}

# ---------------------------------------------------------------------------
# EXPERIMENTO B — Tolerância a falhas
# ---------------------------------------------------------------------------
exp_b() {
    banner "EXPERIMENTO B — Tolerância a Falhas"
    info "Parâmetros: REQUESTS=$REQUESTS  INTERVAL_MS=$INTERVAL_MS"
    echo ""
    echo "  Objetivo: observar o impacto de perder um nó durante o teste."
    echo "    1. Sobe 3 nós em paralelo."
    echo "    2. Após 20s, mata sensor-node-2 (simula falha de gateway)."
    echo "    3. Nós 1 e 3 continuam; analisa throughput e mensagens perdidas."
    echo ""

    build_image

    step "Iniciando 3 nós em background..."
    EXP_REQUESTS=$REQUESTS EXP_INTERVAL_MS=$INTERVAL_MS \
    docker compose --profile sensors up \
        --force-recreate \
        sensor-node-1 sensor-node-2 sensor-node-3 &
    COMPOSE_PID=$!

    info "Aguardando 20s para os nós estabilizarem..."
    sleep 20

    step "Simulando falha: matando sensor-node-2..."
    docker stop sensor-node-2 2>/dev/null && \
        echo -e "${RED}✗  sensor-node-2 derrubado!${RESET}" || \
        warn "sensor-node-2 não estava rodando."

    info "Nós 1 e 3 continuam. Aguardando conclusão..."
    wait $COMPOSE_PID || true   # o exit code pode ser não-zero por causa do kill

    step "Comparando resultados (nós sobreviventes vs esperado)..."

    N1=$(ls -t "$RESULTS_DIR"/load_test_node-1_*.json 2>/dev/null | head -1)
    N3=$(ls -t "$RESULTS_DIR"/load_test_node-3_*.json 2>/dev/null | head -1)

    if [[ -n "$N1" && -n "$N3" ]]; then
        python3 "$SCRIPTS_DIR/compare_results.py" "$N1" "$N3"
    fi

    banner "Experimento B concluído"
    echo "  Interpretação:"
    echo "    • Devices do nó-2 (334–666) perderam mensagens → falha parcial"
    echo "    • Nós 1 e 3 completaram normalmente → isolamento de falha"
    echo "    • Em produção: um load balancer redistribuiria os devices do nó-2"
}

# ---------------------------------------------------------------------------
# EXPERIMENTO C — Local vs Docker (baseline asyncio vs cluster)
# ---------------------------------------------------------------------------
exp_c() {
    banner "EXPERIMENTO C — Local vs Docker"
    echo ""
    echo "  Objetivo: comparar execução direta (asyncio no host) com cluster Docker."
    echo ""
    echo "  O baseline local já está em results/. Selecione o arquivo correto abaixo."
    echo ""

    # Lista os resultados disponíveis para o usuário escolher o baseline
    step "Resultados disponíveis em results/:"
    ls -lt "$RESULTS_DIR"/*.json 2>/dev/null | head -10 | \
        awk '{print "   " NR". " $9}' || warn "Nenhum resultado encontrado."

    echo ""
    read -rp "  Digite o caminho do baseline local (ou Enter para pular): " BASELINE_FILE

    step "Rodando 3 nós Docker com mesmos parâmetros do baseline..."
    EXP_REQUESTS=$REQUESTS EXP_INTERVAL_MS=$INTERVAL_MS \
    docker compose --profile sensors up \
        --force-recreate \
        sensor-node-1 sensor-node-2 sensor-node-3

    N1=$(ls -t "$RESULTS_DIR"/load_test_node-1_*.json 2>/dev/null | head -1)
    N2=$(ls -t "$RESULTS_DIR"/load_test_node-2_*.json 2>/dev/null | head -1)
    N3=$(ls -t "$RESULTS_DIR"/load_test_node-3_*.json 2>/dev/null | head -1)

    if [[ -n "$N1" && -n "$N2" && -n "$N3" ]]; then
        if [[ -n "$BASELINE_FILE" && -f "$BASELINE_FILE" ]]; then
            python3 "$SCRIPTS_DIR/compare_results.py" "$N1" "$N2" "$N3" \
                --baseline "$BASELINE_FILE" --save
        else
            python3 "$SCRIPTS_DIR/compare_results.py" "$N1" "$N2" "$N3" --save
        fi
    fi

    banner "Experimento C concluído"
    echo "  Interpretação:"
    echo "    • Overhead Docker ≈ diferença de latência (tipicamente <5ms em bridge)"
    echo "    • Throughput similar → a rede virtual não é gargalo para MQTT"
    echo "    • Isolamento de processos: cada nó tem seu próprio event loop asyncio"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
EXPERIMENT="${1:-}"

if [[ -z "$EXPERIMENT" ]]; then
    echo "Uso: bash scripts/run_experiments.sh [A|B|C|all]"
    echo ""
    echo "  A   → Escalabilidade horizontal (1 nó vs 3 nós)"
    echo "  B   → Tolerância a falhas (matar sensor-node-2)"
    echo "  C   → Local vs Docker (asyncio direto vs cluster)"
    echo "  all → Executa A, B e C em sequência"
    exit 0
fi

cd "$(dirname "$0")/.."   # garante execução a partir da raiz do projeto
check_prereqs

case "$EXPERIMENT" in
    A|a)   exp_a ;;
    B|b)   exp_b ;;
    C|c)   exp_c ;;
    all)   exp_a; exp_b; exp_c ;;
    *)     error "Experimento desconhecido: '$EXPERIMENT'. Use A, B, C ou all." ;;
esac
