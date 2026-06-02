# Fall Detection IoT - Load Test

Este projeto simula uma frota de dispositivos IoT de deteccao de quedas,
inspirados em sensores ESP32 com MPU6050, enviando telemetria via MQTT para o
ThingsBoard CE. O foco do repositorio e testar carga, distribuicao,
observabilidade e tolerancia a falhas em um cenario de monitoramento de idosos.

A aplicacao pode rodar de duas formas:

- **Modo local:** um processo Python no host cria varias coroutines `asyncio`,
  cada uma representando um device.
- **Modo distribuido:** tres containers `sensor-node-*` rodam em paralelo na rede
  Docker `iot-cloud`, cada um responsavel por uma fatia dos devices.

## Componentes

| Componente | Papel |
|---|---|
| `thingsboard` | Plataforma IoT central. Recebe telemetria MQTT, persiste dados e exibe dashboards. |
| `postgres` | Banco usado pelo ThingsBoard. |
| `scripts/provision_devices.py` | Cria tenant, usuario, device profile, devices e tokens no ThingsBoard. |
| `scripts/load_test.py` | Motor da simulacao. Gera dados de sensores e publica no MQTT. |
| `scripts/dashboard_setup.py` | Gera e cria/atualiza o dashboard do ThingsBoard. |
| `prometheus` | Coleta metricas operacionais expostas pelos processos de carga. |
| `grafana` | Exibe os paineis de throughput, latencia, erros e quedas. |
| `scripts/compare_results.py` | Agrega relatorios JSON de varios nos e compara com baseline. |
| `scripts/run_experiments.sh` | Orquestra os experimentos A, B e C. |
| `scripts/cleanup.py` | Remove os devices criados e apaga `device_tokens.json`. |

## Arquitetura

### Modo local

```text
Host
  scripts/load_test.py
    ├─ coroutine device 1
    ├─ coroutine device 2
    ├─ ...
    └─ coroutine device N
          │
          │ MQTT topic: v1/devices/me/telemetry
          ▼
Docker: thingsboard:1883
          │
          ▼
Docker: postgres
```

No modo local, o processo Python roda fora do Docker e se conecta ao
ThingsBoard por `localhost:1883`. O servidor de metricas Prometheus do
`load_test.py` fica em `localhost:8001`.

### Modo distribuido

```text
rede Docker: iot-cloud

sensor-node-1          sensor-node-2          sensor-node-3
indices 0..333         indices 334..666       indices 667..999
NODE_ID=node-1         NODE_ID=node-2         NODE_ID=node-3
METRICS_PORT=8001      METRICS_PORT=8001      METRICS_PORT=8001
       │                      │                      │
       └────────────── MQTT: thingsboard:1883 ───────┘
                              │
                              ▼
                         thingsboard
                              │
                              ▼
                           postgres

prometheus ── scrape ──> sensor-node-1:8001
prometheus ── scrape ──> sensor-node-2:8001
prometheus ── scrape ──> sensor-node-3:8001
grafana    ── datasource ──> prometheus:9090
```

Cada `sensor-node` e um processo Python independente, com seu proprio event
loop e seu proprio IP dentro da rede Docker. O particionamento e feito com
`DEVICE_OFFSET` e `DEVICE_COUNT`, evitando que dois nos usem o mesmo token ou o
mesmo identificador MQTT.

## Portas

|     Servico      |      URL no host        |                             Observacao                                |
|------------------|-------------------------|-----------------------------------------------------------------------|
| ThingsBoard HTTP | `http://localhost:8090` | O container usa porta interna `9090`; o Compose mapeia `8090:9090`. |
| ThingsBoard MQTT | `localhost:1883`        | Usado pelos simuladores locais. |
| Prometheus       | `http://localhost:9090` | Coleta metricas do load test. |
| Grafana          | `http://localhost:3000` | Login padrao do Compose: `admin` / `admin`. |

## Preparacao

Instale dependencias Python para executar os scripts no host:

```bash
pip install -r requirements.txt
```

Crie um `.env` com as credenciais do Postgres usadas pelo
`docker-compose.yml`:

```env
POSTGRES_USER=thingsboard
POSTGRES_PASSWORD=thingsboard
```

Os scripts tambem aceitam variaveis opcionais como `TB_HOST`,
`TB_HTTP_PORT`, `TB_ADMIN_EMAIL`, `TB_ADMIN_PASSWORD`, `TB_TENANT_EMAIL` e
`TB_TENANT_PASSWORD`. Se nao forem definidas, os principais scripts usam os
defaults do ambiente local do projeto.

## Fluxo completo da aplicacao

### 1. Subida da infraestrutura

```bash
docker compose up thingsboard postgres prometheus grafana -d
```

O fluxo de inicializacao e:

1. O `postgres` sobe e executa o healthcheck `pg_isready`.
2. O `thingsboard` espera o Postgres ficar saudavel.
3. O ThingsBoard usa `TB_QUEUE_TYPE=in-memory` e grava seus dados no Postgres.
4.  oard valida se a porta interna `9090` esta aceitando conexoes.
5. O `prometheus` carrega `monitoring/prometheus.yml`.
6. O `grafana` carrega automaticamente o datasource Prometheus e o dashboard de carga.

Arquivos importantes nessa fase:

- `docker-compose.yml`
- `monitoring/prometheus.yml`
- `monitoring/grafana/provisioning/datasources/prometheus.yml`
- `monitoring/grafana/provisioning/dashboards/load_test.yml`
- `monitoring/grafana/dashboards/load_test.json`

### 2. Provisionamento dos devices no ThingsBoard

```bash
python scripts/provision_devices.py --devices 1000
```

Esse script prepara o ThingsBoard para aceitar telemetria dos simuladores.
Internamente ele faz:

1. Le `config/load_test_config.yaml`.
2. Aguarda o ThingsBoard responder em `http://localhost:8090`.
3. Autentica como sysadmin usando a API REST.
4. Procura ou cria o tenant `Fall Detection Lab`.
5. Procura ou cria o usuario tenant admin configurado em `TB_TENANT_EMAIL`.
6. Obtem um token do usuario tenant por impersonacao via sysadmin.
7. Procura ou cria o device profile `fall-detector`.
8. Configura uma regra de alarme `QUEDA_DETECTADA` quando `fall_detected == true`.
9. Cria devices com nomes como `fall-sensor-000001`.
10. Busca o access token MQTT de cada device.
11. Salva tudo em `device_tokens.json`.

O arquivo `device_tokens.json` e gerado localmente e fica fora do Git. Ele e a
ponte entre o provisionamento HTTP e o envio MQTT: cada device simulado usa seu
token como username na conexao MQTT com o ThingsBoard.

O provisionamento e idempotente. Se um device ja existe, o script reaproveita o
device e recupera suas credenciais em vez de criar duplicado.

### 3. Criacao do dashboard do ThingsBoard

```bash
python scripts/dashboard_setup.py
```

Esse script monta o dashboard operacional dentro do ThingsBoard:

1. Autentica como sysadmin.
2. Localiza o usuario tenant e obtem token por impersonacao.
3. Detecta se ha widget de mapa disponivel na versao do ThingsBoard.
4. Constroi o JSON do dashboard.
5. Salva o JSON em `dashboard/fall_detection_dashboard.json`.
6. Cria ou atualiza o dashboard `Fall Detection IoT - Load Test Dashboard`.

O dashboard usa dois aliases:

- `all_sensors`: resolve todos os devices do profile `fall-detector`.
- `single_device`: resolve o device selecionado no estado do dashboard.

Widgets criados:

- card de magnitude atual;
- card de queda detectada;
- serie temporal da magnitude do device selecionado;
- serie temporal dos eixos `accel_x`, `accel_y` e `accel_z`;
- mapa, ou tabela de fallback, com latitude e longitude dos devices.

### 4. Execucao do load test

Modo local:

```bash
python scripts/load_test.py --devices 1000 --requests 100 --interval 100
```

Modo distribuido:

```bash
docker compose build sensor-node-1

EXP_REQUESTS=100 EXP_INTERVAL_MS=100 \
docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Quando `scripts/load_test.py` inicia, o fluxo interno e:

1. Carrega `.env`, `config/load_test_config.yaml` e argumentos de linha de comando.
2. Le `device_tokens.json`.
3. Filtra apenas tokens validos.
4. Seleciona uma fatia do arquivo usando `--offset` e `--devices`.
5. Inicia um servidor HTTP de metricas Prometheus em `METRICS_PORT` (`8001` por padrao).
6. Cria uma coroutine `asyncio` para cada device.
7. Usa um semaforo com `max_concurrent_connects` para limitar conexoes MQTT simultaneas.
8. Cada coroutine abre um cliente `aiomqtt.Client`.
9. O token do device e usado como `username`.
10. O `identifier` MQTT e montado como `sim-000001`, `sim-000002` etc.
11. A cada intervalo, o simulador gera uma leitura de sensor.
12. O payload e publicado no topico `v1/devices/me/telemetry`.
13. As metricas em memoria e Prometheus sao atualizadas.
14. Ao final, um relatorio JSON e salvo em `results/`.

### 5. Simulacao do sensor e eventos de queda

Cada device possui uma instancia de `SensorSimulator`. Ela simula:

- aceleracao nos eixos `accel_x`, `accel_y` e `accel_z`;
- magnitude da aceleracao;
- ruido gaussiano parecido com leitura real de sensor;
- gravidade em repouso;
- drift lento de GPS ao redor da UFMA em Sao Luis;
- estado de queda com impacto, repouso e confirmacao.

A maquina de estados da queda e:

```text
IDLE
  │ force_fall=true
  ▼
IMPACT_DETECTED
  │ apos o periodo de repouso e magnitude abaixo do limite
  ▼
CONFIRMED
  │ gera fall_detected=true uma vez
  ▼
IDLE
```

Existem dois modos de queda:

| Modo | Comportamento |
|---|---|
| `session` | Cada device tem probabilidade `fall_probability` de sofrer exatamente uma queda durante a sessao. |
| `per_request` | Cada leitura tem probabilidade `fall_probability` de forcar uma queda. |

O modo padrao vem de `config/load_test_config.yaml`, mas pode ser sobrescrito:

```bash
python scripts/load_test.py --fall-mode session --fall-prob 0.05
```

### 6. Payload MQTT enviado

Cada mensagem publicada no ThingsBoard segue este formato:

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

No ThingsBoard, esses campos entram como timeseries do device autenticado pelo
access token MQTT.

### 7. Metricas Prometheus durante o teste

O `load_test.py` expoe metricas em `/metrics`. No modo Docker, o Prometheus
raspa os tres nos pela rede interna:

```text
sensor-node-1:8001
sensor-node-2:8001
sensor-node-3:8001
```

No modo local, o Prometheus tenta raspar:

```text
host.docker.internal:8001
```

Metricas expostas:

| Metrica | Significado |
|---|---|
| `load_test_throughput_msg_per_second` | Throughput atual por no. |
| `load_test_latency_avg_ms` | Latencia media de publicacao MQTT. |
| `load_test_latency_p99_ms` | Latencia p99 aproximada com janela em memoria. |
| `load_test_active_devices` | Coroutines ainda ativas. |
| `load_test_connected_devices` | Devices que ja conseguiram conectar. |
| `load_test_messages_total` | Total de mensagens publicadas. |
| `load_test_errors_total` | Total de erros de conexao/publicacao. |
| `load_test_falls_total` | Total de quedas simuladas. |

O Grafana usa essas metricas para exibir:

- throughput por no e throughput total;
- latencia media e p99;
- devices ativos e conectados;
- taxa de erro;
- quedas simuladas por segundo;
- mensagens publicadas por segundo;
- totais de erros e quedas.

Durante periodos sem `sensor-node-*` em execucao, alguns targets do Prometheus
podem aparecer como down. Isso e esperado: os containers de sensores rodam o
teste e encerram.

### 8. Relatorio final em JSON

Ao terminar, cada no salva um arquivo:

```text
results/load_test_<NODE_ID>_<timestamp>.json
```

Campos principais:

| Campo | Significado |
|---|---|
| `node_id` | Identificacao do no (`local`, `node-1`, `node-2`, etc.). |
| `device_offset` | Inicio da fatia lida em `device_tokens.json`. |
| `total_devices` | Quantidade de devices realmente usados. |
| `requests_per_device` | Mensagens planejadas por device. |
| `interval_ms` | Intervalo entre publicacoes por device. |
| `total_published` | Mensagens publicadas com sucesso. |
| `total_errors` | Erros acumulados. |
| `total_falls_simulated` | Quedas confirmadas pelo simulador. |
| `throughput_msg_per_s` | Throughput medio do no. |
| `avg_latency_ms` | Latencia media de publicacao. |
| `p99_latency_ms` | Latencia p99 aproximada. |
| `error_rate_pct` | Percentual de erro. |

## Execucao rapida

Para um teste pequeno e reproduzivel:

```bash
docker compose up thingsboard postgres prometheus grafana -d
python scripts/provision_devices.py --devices 1000
python scripts/dashboard_setup.py
python scripts/load_test.py --devices 100 --requests 20 --interval 100
```

Depois acesse:

- ThingsBoard: `http://localhost:8090`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`

## Modo distribuido com Docker

Os tres nos definidos no `docker-compose.yml` usam a mesma imagem
`Dockerfile.sensor`. A imagem copia `scripts/load_test.py`, `config/` e
`requirements.txt`. Em runtime, o Compose monta:

- `./device_tokens.json:/app/device_tokens.json:ro`
- `./results:/app/results`

Particionamento padrao por indice dentro de `device_tokens.json`:

| Container | `NODE_ID` | Devices | `DEVICE_OFFSET` | `DEVICE_COUNT` |
|---|---|---:|---:|---:|
| `sensor-node-1` | `node-1` | 0-333 | 0 | 334 |
| `sensor-node-2` | `node-2` | 334-666 | 334 | 333 |
| `sensor-node-3` | `node-3` | 667-999 | 667 | 333 |

Rodar todos os sensores:

```bash
EXP_REQUESTS=50 EXP_INTERVAL_MS=100 \
docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Como `restart: "no"`, os containers finalizam quando o teste termina. Os
relatorios continuam disponiveis em `results/` no host.

## Experimentos

O script `scripts/run_experiments.sh` automatiza cenarios de sistemas
distribuidos.

```bash
bash scripts/run_experiments.sh A
bash scripts/run_experiments.sh B
bash scripts/run_experiments.sh C
bash scripts/run_experiments.sh all
```

Variaveis aceitas:

```bash
REQUESTS=50 INTERVAL_MS=100 DEVICES_TOTAL=1000 bash scripts/run_experiments.sh A
```

### Experimento A - Escalabilidade horizontal

Compara:

- **A1:** um container processando todos os devices;
- **A3:** tres containers processando fatias do mesmo total de devices.

O objetivo e observar se o throughput aumenta quando a carga e dividida entre
nos independentes, mantendo latencia parecida.

### Experimento B - Tolerancia a falhas

Sobe tres nos, espera 20 segundos e para `sensor-node-2` com `docker stop`.
O objetivo e observar falha parcial: os devices da fatia do no 2 param de
publicar, enquanto os nos 1 e 3 continuam.

### Experimento C - Local vs Docker

Compara um baseline local ja salvo em `results/` com uma execucao distribuida
em containers. O objetivo e medir overhead da rede bridge Docker e do isolamento
por processo/container.

## Comparacao de resultados

Agregue relatorios de varios nos:

```bash
python scripts/compare_results.py results/load_test_node-*.json
```

Use os arquivos mais recentes:

```bash
python scripts/compare_results.py --latest 3
```

Compare com baseline:

```bash
python scripts/compare_results.py results/load_test_node-*.json \
  --baseline results/load_test_local_<timestamp>.json
```

Salve o agregado:

```bash
python scripts/compare_results.py results/load_test_node-*.json --save
```

Regras de agregacao:

| Metrica | Regra |
|---|---|
| Throughput | Soma dos throughputs dos nos. |
| Latencia media | Media ponderada por `total_published`. |
| Latencia p99 | Maximo entre os nos, como limite conservador. |
| Duracao | Maximo entre os nos. |
| Mensagens, erros e quedas | Soma. |

## Configuracao principal

Arquivo: `config/load_test_config.yaml`

```yaml
load_test:
  n_devices: 100000
  requests_per_device: 100
  interval_ms: 100
  fall_probability: 0.05
  fall_mode: "session"
  max_concurrent_connects: 5000
  connect_delay_ms: 0
  provision_concurrency: 150

sensor:
  fall_threshold_g: 2.0
  rest_threshold_g: 0.3
  rest_duration_ms: 1500
  accel_range_g: 4.0
  noise_std_g: 0.02
  gravity_g: 1.0

location:
  base_lat: -2.5489
  base_lon: -44.2029
  drift_radius_km: 1.5
```

Observacao: o default `n_devices: 100000` e uma carga alta. Para testes
rapidos, prefira passar `--devices` no `load_test.py` e `--devices` no
`provision_devices.py`.

## Variaveis de ambiente uteis

| Variavel | Usada por | Padrao | Funcao |
|---|---|---|---|
| `POSTGRES_USER` | Compose | sem padrao | Usuario do Postgres. |
| `POSTGRES_PASSWORD` | Compose | sem padrao | Senha do Postgres. |
| `TB_HOST` | Scripts Python | `localhost` | Host HTTP/MQTT do ThingsBoard. |
| `TB_HTTP_PORT` | Provision/dashboard/debug | `8090` | Porta HTTP do ThingsBoard no host. |
| `TB_MQTT_PORT` | Load test | `1883` | Porta MQTT do ThingsBoard. |
| `TB_ADMIN_EMAIL` | Provision/dashboard/debug | `sysadmin@thingsboard.org` | Usuario sysadmin do ThingsBoard. |
| `TB_ADMIN_PASSWORD` | Provision/dashboard/debug | `sysadmin` | Senha sysadmin. |
| `TB_TENANT_EMAIL` | Provision/dashboard/cleanup | `tenant@thingsboard.org` | Usuario tenant admin usado pelo projeto. |
| `TB_TENANT_PASSWORD` | Cleanup | `tenant2026` | Senha usada pelo cleanup para login tenant. |
| `NODE_ID` | Load test | `local` | Nome do no no log, metricas e relatorio. |
| `DEVICE_OFFSET` | Load test | `0` | Inicio da fatia de tokens. |
| `DEVICE_COUNT` | Dockerfile/Compose | `333` no Dockerfile; varia por no no Compose | Quantidade de devices por container. |
| `METRICS_PORT` | Load test | `8001` | Porta HTTP do endpoint Prometheus. |
| `EXP_REQUESTS` | Compose | `1000` | Requests por device nos sensores Docker. |
| `EXP_INTERVAL_MS` | Compose | `100` | Intervalo por device nos sensores Docker. |
| `REQUESTS` | `run_experiments.sh` | `100` | Requests por device nos experimentos. |
| `INTERVAL_MS` | `run_experiments.sh` | `100` | Intervalo nos experimentos. |
| `DEVICES_TOTAL` | `run_experiments.sh` | `1000` | Total de devices no experimento A. |

Observacao sobre `cleanup.py`: o Compose expoe o ThingsBoard em `8090`, mas o
fallback atual do script e `8080`. Se for usar o cleanup sem alterar o script,
execute com `TB_HTTP_PORT=8090`.

## Arquivos gerados

| Arquivo/diretorio | Quando surge | Conteudo |
|---|---|---|
| `device_tokens.json` | Provisionamento | IDs e access tokens dos devices. |
| `dashboard/fall_detection_dashboard.json` | Setup do dashboard | JSON enviado ao ThingsBoard. |
| `results/load_test_<node>_<ts>.json` | Fim de cada load test | Relatorio individual do no. |
| `results/cluster_<ts>.json` | `compare_results.py --save` | Relatorio agregado do cluster. |

`device_tokens.json`, `results/` e `.env` sao ignorados pelo Git.

## Limpeza e diagnostico

Remover devices provisionados:

```bash
TB_HTTP_PORT=8090 python scripts/cleanup.py
```

Sem confirmacao interativa:

```bash
TB_HTTP_PORT=8090 python scripts/cleanup.py --yes
```

Inspecionar tenants e usuarios reais do ThingsBoard:

```bash
python scripts/debug_tb.py
```

## Estrutura do projeto

```text
load-test-fall-detection-iot/
├── docker-compose.yml
├── Dockerfile.sensor
├── requirements.txt
├── config/
│   └── load_test_config.yaml
├── scripts/
│   ├── provision_devices.py
│   ├── load_test.py
│   ├── dashboard_setup.py
│   ├── compare_results.py
│   ├── run_experiments.sh
│   ├── cleanup.py
│   └── debug_tb.py
├── dashboard/
│   └── fall_detection_dashboard.json
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
│       ├── dashboards/load_test.json
│       └── provisioning/
│           ├── dashboards/load_test.yml
│           └── datasources/prometheus.yml
└── results/
    └── arquivos JSON gerados em runtime
```
