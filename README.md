# Fall Detection IoT — Load Test

Simula dispositivos ESP32 publicando telemetria de queda de idosos via MQTT
para um ThingsBoard CE. Suporta dois modos de execução:

- **Local** — asyncio direto no host (baseline)
- **Distribuído** — múltiplos containers Docker na rede virtual `iot-cloud`, cada um
  responsável por uma fatia do pool de devices (simula gateways regionais em nuvem)

---

## Arquitetura

### Modo local (baseline)

```
Python asyncio (N coroutines)
  └─ aiomqtt → MQTT :1883
                    │
       ┌────────────▼───────────────────┐
       │  Docker: ThingsBoard CE        │
       │  :8090 HTTP  |  :1883 MQTT     │
       │  PostgreSQL (interno)          │
       └────────────────────────────────┘
```

### Modo distribuído (Docker)

```
┌────────────────────────── rede: iot-cloud (bridge) ──────────────────────────┐
│                                                                               │
│  sensor-node-1          sensor-node-2          sensor-node-3                 │
│  devices [0–333]        devices [334–666]       devices [667–999]            │
│  IP próprio             IP próprio              IP próprio                    │
│       │                      │                       │                       │
│       └──────────────────────┼───────────────────────┘                       │
│                              │ MQTT (DNS interno: thingsboard:1883)           │
│                  ┌───────────▼──────────────┐                                │
│                  │  thingsboard             │                                 │
│                  │  :9090 HTTP | :1883 MQTT │                                 │
│                  └───────────┬──────────────┘                                │
│                              │                                                │
│                  ┌───────────▼──────────────┐                                │
│                  │  postgres  :5432          │                                │
│                  └──────────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────────────┘
```

Cada `sensor-node` é um processo Python independente com seu próprio IP,
exatamente como gateways regionais em nuvem real. O ThingsBoard os enxerga
como três clientes MQTT distintos originados de endereços diferentes.

---

## Pré-requisitos

- Docker + Docker Compose
- Python 3.11+
- ~2 GB de RAM livres para o ThingsBoard

```bash
pip install -r requirements.txt
```

---

## Modo Local (uso básico)

### 1. Subir o ThingsBoard

```bash
docker compose up thingsboard postgres -d
```

Aguardar inicialização (~90s):

```bash
docker compose logs -f thingsboard
# Pronto quando aparecer: "Started Application in X seconds"
```

### 2. Provisionar devices

```bash
python scripts/provision_devices.py
```

Cria os devices no ThingsBoard via REST API e salva os tokens em
`device_tokens.json`. Idempotente — pode ser re-executado com segurança.

Se o provisionamento ficar repetindo conexão recusada ou o log do ThingsBoard
mostrar `password authentication failed for user "thingsboard"`, o volume do
Postgres provavelmente foi criado com uma senha antiga. Sincronize a senha do
banco com o `.env` e reinicie:

```bash
docker compose exec -T postgres psql -U thingsboard -d thingsboard \
  -c "ALTER USER thingsboard WITH PASSWORD 'thingsboard2026'"
docker compose restart thingsboard
```

### 3. Criar dashboard

```bash
python scripts/dashboard_setup.py
```

Acesse: **http://localhost:8090**
- Email: `tenant@thingsboard.org`
- Senha: `tenant2026`

### 4. Rodar o load test

```bash
# Padrão (valores do config.yaml)
python scripts/load_test.py

# Customizado
python scripts/load_test.py --devices 1000 --requests 100 --interval 100

# Nó parcial (ex: processar só os devices 0–499)
python scripts/load_test.py --devices 500 --offset 0
```

O relatório JSON é salvo em `results/load_test_<NODE_ID>_<timestamp>.json`.

---

## Modo Distribuído (Docker)

### 1. Build da imagem sensor

```bash
# Basta uma vez; as 3 replicas usam a mesma imagem
docker compose build sensor-node-1
```

### 2. Subir infraestrutura e provisionar devices

```bash
# Só infraestrutura (ThingsBoard + Postgres):
docker compose up thingsboard postgres -d

# Gera device_tokens.json, usado pelos containers sensores:
python scripts/provision_devices.py
```

### 3. Rodar os nós

```bash
# Todos os nós em paralelo (requer profile "sensors"):
docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Os 3 containers rodam simultaneamente, cada um publicando sua fatia:

| Container       | Devices   | `DEVICE_OFFSET`| `DEVICE_COUNT` |
|-----------------|-----------|----------------|----------------|
| `sensor-node-1` | 0 – 333   | 0              | 334            |
| `sensor-node-2` | 334 – 666 | 334            | 333            |
| `sensor-node-3` | 667 – 999 | 667            | 333            |

### 4. Customizar parâmetros sem editar arquivos

As variáveis `EXP_REQUESTS` e `EXP_INTERVAL_MS` propagam para todos os nós:

```bash
EXP_REQUESTS=50 EXP_INTERVAL_MS=200 \
  docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

### 5. Simular latência com Traffic Control

Os containers sensores podem aplicar Linux Traffic Control (`tc netem`) antes de
iniciar o `load_test.py`. Isso permite simular latência, jitter, perda de
pacotes e limite de banda na interface de rede do container.

Após alterar o Dockerfile, reconstrua a imagem:

```bash
docker compose build sensor-node-1
```

Exemplo com 100 ms de latência artificial em todos os nós sensores:

```bash
TC_LATENCY_MS=100 \
  docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Exemplo com latência, jitter e perda:

```bash
TC_LATENCY_MS=120 TC_JITTER_MS=30 TC_LOSS_PCT=0.5 \
  EXP_REQUESTS=50 EXP_INTERVAL_MS=100 \
  docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Exemplo limitando banda:

```bash
TC_RATE=1mbit \
  docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Variáveis disponíveis:

| Variável | Padrão | Efeito |
|---|---:|---|
| `TC_LATENCY_MS` | `0` | Atraso fixo adicionado aos pacotes de saída do container. |
| `TC_JITTER_MS` | `0` | Variação da latência, usando distribuição normal do netem. |
| `TC_LOSS_PCT` | `0` | Percentual de perda de pacotes. Ex: `0.5` para 0,5%. |
| `TC_RATE` | vazio | Limite de banda aceito pelo `tc`, como `1mbit` ou `500kbit`. |
| `TC_IFACE` | `eth0` | Interface do container onde o netem será aplicado. |
| `TC_STRICT` | `true` | Se `true`, falha o container caso o `tc` não seja aplicado. |

O `docker-compose.yml` adiciona `cap_add: NET_ADMIN` aos sensores, pois o
`tc qdisc` precisa dessa capacidade para alterar a fila da interface. Quando
todas as variáveis de Traffic Control ficam zeradas/vazias, o container apenas
informa que o `tc` está desativado e roda normalmente.

---

## Experimentos de Sistemas Distribuídos

O script `run_experiments.sh` orquestra três experimentos automaticamente.

```bash
# Experimento A — escalabilidade horizontal
bash scripts/run_experiments.sh A

# Experimento B — tolerância a falhas
bash scripts/run_experiments.sh B

# Experimento C — local vs Docker
bash scripts/run_experiments.sh C

# Todos em sequência
bash scripts/run_experiments.sh all

# Com carga menor (recomendado para testes rápidos):
REQUESTS=50 INTERVAL_MS=100 bash scripts/run_experiments.sh A

# Com latência artificial nos nós Docker:
TC_LATENCY_MS=100 TC_JITTER_MS=20 REQUESTS=50 bash scripts/run_experiments.sh A
```

### Experimento A — Escalabilidade Horizontal

Roda o mesmo total de devices em duas configurações e compara:

```
A1: 1 container  →  1000 devices num único processo Python
A3: 3 containers →  334 + 333 + 333 devices em paralelo
```

**O que observar:** throughput do cluster A3 deve ser próximo de 3× o de A1.
Latência deve permanecer similar — a distribuição não adiciona overhead significativo.

### Experimento B — Tolerância a Falhas

Sobe 3 nós, aguarda 20s e mata `sensor-node-2` com `docker stop`.

**O que observar:** devices 334–666 perdem mensagens (falha parcial).
Nós 1 e 3 completam normalmente — **isolamento de falha**. Em produção,
um load balancer redistribuiria os devices do nó perdido.

### Experimento C — Local vs Docker

Compara a execução asyncio direta no host com o cluster de containers.

**O que observar:** overhead da rede bridge Docker é tipicamente < 5ms.
Cada container tem seu próprio event loop asyncio — melhor isolamento de CPU.

---

## Comparando Resultados

```bash
# Agregar métricas dos 3 nós num único relatório de cluster:
python scripts/compare_results.py results/load_test_node-*.json

# Comparar cluster vs baseline local:
python scripts/compare_results.py results/load_test_node-*.json \
    --baseline results/load_test_local_<timestamp>.json

# Pegar os N arquivos mais recentes automaticamente:
python scripts/compare_results.py --latest 3

# Salvar relatório agregado em JSON:
python scripts/compare_results.py results/load_test_node-*.json --save
```

### Métricas de agregação do cluster

| Métrica | Regra |
|---|---|
| Throughput | Soma (nós paralelos contribuem independentemente) |
| Latência média | Média ponderada pelo `total_published` de cada nó |
| Latência p99 | Máximo entre os nós (limite conservador) |
| Duração | Máximo (teste acaba quando o nó mais lento termina) |
| Msgs / Erros / Quedas | Soma |

---

## Payload MQTT (idêntico ao firmware real)

```json
{
  "accel_x": 0.0231,
  "accel_y": -0.0145,
  "accel_z": 1.0012,
  "magnitude": 1.0014,
  "fall_detected": false,
  "impact_magnitude": 0.0,
  "latitude": -2.5489,
  "longitude": -44.2029,
  "status": "normal",
  "device_id": "fall-sensor-000001"
}
```

---

## Configuração

`config/load_test_config.yaml`:

```yaml
load_test:
  n_devices: 1000
  requests_per_device: 100
  interval_ms: 100          # igual ao ESP32 real
  fall_probability: 0.05    # 5% dos devices terão 1 queda na sessão
  fall_mode: "session"      # "session" | "per_request"
  max_concurrent_connects: 5000
```

---

## Estrutura

```
load-test-fall-detection-iot/
├── docker-compose.yml          # ThingsBoard + Postgres + 3 sensor-nodes
├── Dockerfile.sensor           # Imagem do nó sensor distribuído
├── .env                        # Credenciais (não commitado)
├── requirements.txt
├── config/
│   └── load_test_config.yaml
├── scripts/
│   ├── provision_devices.py    # Cria devices via REST API
│   ├── load_test.py            # Motor MQTT (asyncio + --offset para particionamento)
│   ├── sensor_entrypoint.py    # Aplica tc/netem antes de iniciar o load test
│   ├── compare_results.py      # Agrega e compara relatórios de múltiplos nós
│   ├── run_experiments.sh      # Orquestra experimentos A, B e C
│   ├── dashboard_setup.py      # Cria dashboard com widgets
│   └── cleanup.py              # Remove devices de teste
├── dashboard/
│   └── fall_detection_dashboard.json
└── results/
    ├── load_test_<node-id>_<timestamp>.json   # Relatório por nó
    └── cluster_<timestamp>.json               # Relatório agregado do cluster
```

---

## Baseline de referência

Execução local com asyncio direto no host:

| Métrica | Valor |
|---|---|
| Devices | 1 000 |
| Total mensagens | 1 000 000 |
| Throughput | 816,5 msg/s |
| Latência média | 14,6 ms |
| Latência p99 | 71,5 ms |
| Taxa de erro | 0 % |
