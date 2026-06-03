"""
provision_devices.py
--------------------
Cria N dispositivos simulados no ThingsBoard via REST API e salva
os access tokens em device_tokens.json para uso no load test.

Para 100 000 devices usa criação paralela com semáforo (provision_concurrency
no config). Exemplo de tempo: ~150 req/s × 600s = 90 000 req em 10 minutos.

Uso:
    python3 scripts/provision_devices.py
    python3 scripts/provision_devices.py --devices 1000
    python3 scripts/provision_devices.py --devices 100000 --concurrency 200
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

load_dotenv(Path(__file__).parent.parent / ".env")

console = Console()

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "load_test_config.yaml"
TOKENS_PATH = BASE_DIR / "device_tokens.json"

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

TB_HOST = os.getenv("TB_HOST", "localhost")
TB_HTTP_PORT = os.getenv("TB_HTTP_PORT", "8090")
TB_BASE_URL = f"http://{TB_HOST}:{TB_HTTP_PORT}"
ADMIN_EMAIL = os.getenv("TB_ADMIN_EMAIL", "sysadmin@thingsboard.org")
ADMIN_PASSWORD = os.getenv("TB_ADMIN_PASSWORD", "sysadmin")
TENANT_EMAIL = os.getenv("TB_TENANT_EMAIL", "tenant@thingsboard.org")

# Intervalo entre saves parciais (em número de devices criados)
SAVE_EVERY = 500


# ---------------------------------------------------------------------------
# Helpers de autenticação
# ---------------------------------------------------------------------------

def _response_error(resp: httpx.Response) -> str:
    """Resumo curto da resposta HTTP, sem despejar corpos enormes no terminal."""
    try:
        body = resp.json()
        message = body.get("message") or body.get("error") or body
    except Exception:
        message = resp.text.strip()

    if message:
        return f"HTTP {resp.status_code}: {str(message)[:180]}"
    return f"HTTP {resp.status_code}"


async def wait_for_thingsboard(client: httpx.AsyncClient, timeout_s: int = 180) -> None:
    """Aguarda o ThingsBoard inicializar completamente."""
    console.print("[cyan]Aguardando ThingsBoard inicializar...[/cyan]")
    deadline = time.monotonic() + timeout_s
    attempt = 0
    last_error = ""
    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = await client.post(
                f"{TB_BASE_URL}/api/auth/login",
                json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                timeout=5,
            )
            if resp.status_code == 200:
                console.print(f"[green]ThingsBoard pronto! (tentativa {attempt})[/green]")
                return
            last_error = _response_error(resp)
            if resp.status_code in {401, 403}:
                raise RuntimeError(
                    "ThingsBoard respondeu, mas rejeitou as credenciais admin. "
                    "Confira TB_ADMIN_EMAIL/TB_ADMIN_PASSWORD no .env. "
                    f"Resposta: {last_error}"
                )
        except httpx.RequestError as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
        console.print(
            f"[yellow]  Tentativa {attempt}: {last_error or 'sem resposta'}; "
            "aguardando 5s...[/yellow]"
        )
        await asyncio.sleep(5)
    raise TimeoutError(
        f"ThingsBoard não ficou pronto em {timeout_s}s. Última falha: {last_error}"
    )


async def login_admin(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{TB_BASE_URL}/api/auth/login",
        json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


# ---------------------------------------------------------------------------
# Tenant e usuário
# ---------------------------------------------------------------------------

async def get_or_create_tenant(client: httpx.AsyncClient, admin_token: str) -> str:
    """Retorna o tenant_id. Cria o tenant se não existir."""
    headers = {"X-Authorization": f"Bearer {admin_token}"}

    resp = await client.get(
        f"{TB_BASE_URL}/api/tenants?pageSize=20&page=0",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    for t in resp.json().get("data", []):
        if t.get("email") == TENANT_EMAIL:
            console.print(f"[yellow]Tenant existente: {t['id']['id']}[/yellow]")
            return t["id"]["id"]

    resp = await client.post(
        f"{TB_BASE_URL}/api/tenant",
        headers=headers,
        json={
            "title": "Fall Detection Lab",
            "email": TENANT_EMAIL,
            "country": "BR",
            "state": "MA",
            "city": "São Luís",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tenant_id = resp.json()["id"]["id"]
    console.print(f"[green]Tenant criado: {tenant_id}[/green]")
    return tenant_id


async def find_user_across_tenants(
    client: httpx.AsyncClient, admin_token: str
) -> str:
    headers = {"X-Authorization": f"Bearer {admin_token}"}
    r = await client.get(
        f"{TB_BASE_URL}/api/tenants?pageSize=100&page=0",
        headers=headers, timeout=30,
    )
    if r.status_code != 200:
        return ""
    for tenant in r.json().get("data", []):
        tid = tenant["id"]["id"]
        r2 = await client.get(
            f"{TB_BASE_URL}/api/tenant/{tid}/users?pageSize=100&page=0",
            headers=headers, timeout=30,
        )
        if r2.status_code != 200:
            continue
        for user in r2.json().get("data", []):
            if user.get("email") == TENANT_EMAIL:
                uid = user["id"]["id"]
                console.print(
                    f"[yellow]Usuário encontrado no tenant {tid}: {uid}[/yellow]"
                )
                return uid
    return ""


async def get_or_create_tenant_user(
    client: httpx.AsyncClient, admin_token: str, tenant_id: str
) -> str:
    headers = {"X-Authorization": f"Bearer {admin_token}"}

    r = await client.get(
        f"{TB_BASE_URL}/api/tenant/{tenant_id}/users?pageSize=100&page=0",
        headers=headers, timeout=30,
    )
    if r.status_code == 200:
        for user in r.json().get("data", []):
            if user.get("email") == TENANT_EMAIL:
                uid = user["id"]["id"]
                console.print(f"[yellow]Usuário existente no tenant: {uid}[/yellow]")
                return uid

    create_resp = await client.post(
        f"{TB_BASE_URL}/api/user?sendActivationMail=false",
        headers=headers,
        json={
            "tenantId": {"entityType": "TENANT", "id": tenant_id},
            "email": TENANT_EMAIL,
            "authority": "TENANT_ADMIN",
            "firstName": "Fall",
            "lastName": "Detection",
        },
        timeout=30,
    )

    if create_resp.status_code == 200:
        user_id = create_resp.json()["id"]["id"]
        console.print(f"[green]Usuário tenant criado: {user_id}[/green]")
        return user_id

    body = create_resp.json()
    if create_resp.status_code == 400 and "already present" in body.get("message", ""):
        console.print(
            "[yellow]Email já existe em outro tenant. "
            "Varrendo todos os tenants para localizar o user_id...[/yellow]"
        )
        uid = await find_user_across_tenants(client, admin_token)
        if uid:
            return uid

    create_resp.raise_for_status()
    return ""


async def get_tenant_token(
    client: httpx.AsyncClient, admin_token: str, user_id: str
) -> str:
    headers = {"X-Authorization": f"Bearer {admin_token}"}
    resp = await client.get(
        f"{TB_BASE_URL}/api/user/{user_id}/token",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


# ---------------------------------------------------------------------------
# Device Profile
# ---------------------------------------------------------------------------

async def get_or_create_device_profile(
    client: httpx.AsyncClient, tenant_token: str
) -> str:
    headers = {"X-Authorization": f"Bearer {tenant_token}"}
    profile_name = CONFIG["thingsboard"]["device_profile"]

    resp = await client.get(
        f"{TB_BASE_URL}/api/deviceProfiles?pageSize=50&page=0",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    for profile in resp.json().get("data", []):
        if profile["name"] == profile_name:
            console.print(f"[yellow]Device Profile existente: {profile['id']['id']}[/yellow]")
            return profile["id"]["id"]

    resp = await client.post(
        f"{TB_BASE_URL}/api/deviceProfile",
        headers=headers,
        json={
            "name": profile_name,
            "type": "DEFAULT",
            "transportType": "MQTT",
            "provisionType": "DISABLED",
            "profileData": {
                "configuration": {"type": "DEFAULT"},
                "transportConfiguration": {"type": "DEFAULT"},
                "alarms": [
                    {
                        "id": "fall_alarm",
                        "alarmType": "QUEDA_DETECTADA",
                        "createRules": {
                            "CRITICAL": {
                                "condition": {
                                    "condition": [
                                        {
                                            "key": {
                                                "type": "TIME_SERIES",
                                                "key": "fall_detected",
                                            },
                                            "valueType": "BOOLEAN",
                                            "predicate": {
                                                "type": "BOOLEAN",
                                                "operation": "EQUAL",
                                                "value": {"defaultValue": True},
                                            },
                                        }
                                    ],
                                    "spec": {"type": "SIMPLE"},
                                },
                                "schedule": None,
                                "alarmDetails": "",
                            }
                        },
                        "propagate": False,
                    }
                ],
            },
            "default": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    profile_id = resp.json()["id"]["id"]
    console.print(f"[green]Device Profile '{profile_name}' criado: {profile_id}[/green]")
    return profile_id


# ---------------------------------------------------------------------------
# Criação de dispositivos
# ---------------------------------------------------------------------------

async def create_device(
    client: httpx.AsyncClient,
    tenant_token: str,
    profile_id: str,
    name: str,
) -> tuple:
    """Retorna (device_id, access_token). Inclui retry com backoff."""
    headers = {"X-Authorization": f"Bearer {tenant_token}"}

    for attempt in range(3):
        resp = await client.post(
            f"{TB_BASE_URL}/api/device",
            headers=headers,
            json={
                "name": name,
                "deviceProfileId": {"entityType": "DEVICE_PROFILE", "id": profile_id},
            },
            timeout=30,
        )
        if resp.status_code == 200:
            break
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code == 400 and "already exists" in str(body).lower():
            r2 = await client.get(
                f"{TB_BASE_URL}/api/tenant/devices?pageSize=1&page=0&textSearch={name}",
                headers=headers, timeout=30,
            )
            if r2.status_code == 200:
                for dev in r2.json().get("data", []):
                    if dev.get("name") == name:
                        device_id = dev["id"]["id"]
                        r3 = await client.get(
                            f"{TB_BASE_URL}/api/device/{device_id}/credentials",
                            headers=headers, timeout=30,
                        )
                        if r3.status_code == 200:
                            return device_id, r3.json()["credentialsId"]
        if attempt < 2:
            wait = (attempt + 1) * 2
            await asyncio.sleep(wait)
        else:
            raise Exception(f"status {resp.status_code} body={body}")

    device_id = resp.json()["id"]["id"]
    resp2 = await client.get(
        f"{TB_BASE_URL}/api/device/{device_id}/credentials",
        headers=headers, timeout=30,
    )
    resp2.raise_for_status()
    token = resp2.json()["credentialsId"]
    return device_id, token


# ---------------------------------------------------------------------------
# Provisionamento paralelo
# ---------------------------------------------------------------------------

async def provision_parallel(
    client: httpx.AsyncClient,
    tenant_token: str,
    profile_id: str,
    names_to_create: list,
    existing: dict,
    concurrency: int,
    progress,
    task_id,
) -> dict:
    """
    Cria todos os devices em paralelo, limitado por `concurrency` requisições
    simultâneas. Salva progresso em disco a cada SAVE_EVERY devices.
    """
    results = dict(existing)
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    save_counter = [0]

    async def create_one(name: str) -> None:
        async with semaphore:
            try:
                device_id, token = await create_device(
                    client, tenant_token, profile_id, name
                )
                entry = {"device_id": device_id, "token": token}
            except Exception as e:
                entry = {"device_id": None, "token": None, "error": str(e)}

            async with lock:
                results[name] = entry
                save_counter[0] += 1
                progress.advance(task_id)

                if save_counter[0] % SAVE_EVERY == 0:
                    with open(TOKENS_PATH, "w") as f:
                        json.dump(results, f, indent=2)

    await asyncio.gather(*[create_one(name) for name in names_to_create])

    # Save final
    with open(TOKENS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(n_devices: int, concurrency: int) -> None:
    prefix = CONFIG["thingsboard"]["device_name_prefix"]

    console.rule("[bold cyan]Provisionamento de Dispositivos[/bold cyan]")
    console.print(f"ThingsBoard:       [bold]{TB_BASE_URL}[/bold]")
    console.print(f"Dispositivos:      [bold]{n_devices:,}[/bold]")
    console.print(f"Concorrência HTTP: [bold]{concurrency}[/bold] req/simultâneas")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        await wait_for_thingsboard(client)

        console.print("\n[cyan]Autenticando como sysadmin...[/cyan]")
        admin_token = await login_admin(client)
        console.print("[green]OK[/green]")

        console.print("[cyan]Verificando tenant...[/cyan]")
        tenant_id = await get_or_create_tenant(client, admin_token)

        console.print("[cyan]Verificando usuário tenant...[/cyan]")
        user_id = await get_or_create_tenant_user(client, admin_token, tenant_id)

        console.print("[cyan]Obtendo token do tenant via impersonação...[/cyan]")
        tenant_token = await get_tenant_token(client, admin_token, user_id)
        console.print("[green]OK[/green]")

        console.print("[cyan]Verificando device profile...[/cyan]")
        profile_id = await get_or_create_device_profile(client, tenant_token)
        console.print("[green]OK[/green]")

        existing = {}
        if TOKENS_PATH.exists():
            with open(TOKENS_PATH) as f:
                existing = json.load(f)
            console.print(
                f"[yellow]{len(existing):,} devices já existem, pulando...[/yellow]"
            )

        # Usa 6 dígitos para suportar até 999 999 devices
        names_to_create = [
            f"{prefix}-{i:06d}"
            for i in range(1, n_devices + 1)
            if f"{prefix}-{i:06d}" not in existing
        ]

        if not names_to_create:
            console.print("[green]Todos os devices já estão provisionados![/green]")
            return

        console.print(
            f"\n[cyan]Criando {len(names_to_create):,} dispositivos "
            f"({concurrency} em paralelo)...[/cyan]"
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                "Criando devices...", total=len(names_to_create)
            )
            results = await provision_parallel(
                client, tenant_token, profile_id,
                names_to_create, existing,
                concurrency, progress, task_id,
            )

    success = sum(1 for v in results.values() if v.get("token"))
    console.print(f"\n[bold green]Provisionamento concluído![/bold green]")
    console.print(f"  Sucesso:  {success:,}/{n_devices:,}")
    console.print(f"  Tokens:   {TOKENS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fall Detection IoT — Provisionamento")
    parser.add_argument(
        "--devices",
        type=int,
        default=CONFIG["load_test"]["n_devices"],
        help="Número de dispositivos a criar (padrão: valor do config)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=CONFIG["load_test"].get("provision_concurrency", 150),
        help="Requisições HTTP paralelas ao criar devices (padrão: provision_concurrency do config)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.devices, args.concurrency))
