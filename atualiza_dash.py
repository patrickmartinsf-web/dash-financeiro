#!/usr/bin/env python3
"""
atualiza_dash.py — Puxa dados do Pluggy (XP + Itaú) e atualiza o dashboard.

Uso:
  1. Copie .env.example para .env e preencha suas credenciais
  2. pip3 install -r requirements.txt
  3. python3 atualiza_dash.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ──
PLUGGY_BASE = "https://api.pluggy.ai"
CLIENT_ID = os.getenv("PLUGGY_CLIENT_ID")
CLIENT_SECRET = os.getenv("PLUGGY_CLIENT_SECRET")
# IDs dos itens (conexões bancárias) separados por vírgula no .env
# Ex: PLUGGY_ITEM_IDS=uuid1,uuid2
_item_ids_raw = os.getenv("PLUGGY_ITEM_IDS", "")
ITEM_IDS = [i.strip() for i in _item_ids_raw.split(",") if i.strip()]
DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "index.html")

# ── CATEGORIAS ──
# Mapeamento de palavras-chave na descrição → categoria do dashboard
CATEGORIAS = {
    "uber":        {"cat": "uber",        "keywords": ["uber", "99app", "99 app", "99pop", "99taxis"]},
    "aluguel":     {"cat": "aluguel",     "keywords": ["gma imob", "aluguel", "imobili"]},
    "carro":       {"cat": "carro",       "keywords": ["tokio marine", "jambeiro", "financiamento", "combusti", "posto", "shell", "ipiranga"]},
    "lazer":       {"cat": "lazer",       "keywords": ["hotel", "booking", "windsor", "venit", "airbnb", "ingresso", "cinema", "teatro", "meep"]},
    "alimentacao": {"cat": "alimentacao", "keywords": ["restaur", "pizz", "boteco", "ifood", "rappi", "padaria", "supermercado", "mercado", "farmacia", "drogaria"]},
    "assinatura":  {"cat": "assinatura",  "keywords": ["spotify", "netflix", "capcut", "apple.com", "google one", "adobe", "chatgpt", "openai", "icloud", "disney"]},
    "educacao":    {"cat": "educacao",    "keywords": ["facilit", "curso", "escola", "natacao", "rio acqua", "smart fit", "academia", "muay thai"]},
    "telecom":     {"cat": "telecom",     "keywords": ["claro", "light", "energia", "telefon"]},
}


def log(msg):
    print(f"  → {msg}")


def erro(msg):
    print(f"\n❌ {msg}")
    sys.exit(1)


# ══════════════════════════════════════
# PLUGGY API
# ══════════════════════════════════════

def pluggy_auth():
    """Autentica na API do Pluggy e retorna o token."""
    if not CLIENT_ID or not CLIENT_SECRET:
        erro("PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET não definidos no .env")

    log("Autenticando no Pluggy...")
    r = requests.post(f"{PLUGGY_BASE}/auth", json={
        "clientId": CLIENT_ID,
        "clientSecret": CLIENT_SECRET,
    })
    if r.status_code != 200:
        erro(f"Falha na autenticação: {r.status_code} — {r.text}")

    token = r.json().get("apiKey")
    if not token:
        erro("Token não encontrado na resposta do Pluggy")
    log("Autenticado com sucesso")
    return token


def pluggy_headers(token):
    return {
        "X-API-KEY": token,
        "Content-Type": "application/json",
    }


def pluggy_get(token, path, params=None):
    """GET genérico na API do Pluggy com paginação."""
    all_results = []
    page = 1
    while True:
        p = {**(params or {}), "page": page, "pageSize": 500}
        r = requests.get(f"{PLUGGY_BASE}{path}", headers=pluggy_headers(token), params=p)
        if r.status_code != 200:
            erro(f"Erro GET {path}: {r.status_code} — {r.text}")
        data = r.json()
        results = data.get("results", [])
        all_results.extend(results)
        total = data.get("totalPages", 1)
        if page >= total:
            break
        page += 1
    return all_results


def pluggy_get_item(token, item_id):
    """Busca um item específico pelo ID."""
    r = requests.get(f"{PLUGGY_BASE}/items/{item_id}", headers=pluggy_headers(token))
    if r.status_code != 200:
        erro(f"Erro ao buscar item {item_id}: {r.status_code} — {r.text}")
    return r.json()


def listar_items(token):
    """Busca as conexões bancárias pelos IDs definidos no .env."""
    if not ITEM_IDS:
        erro(
            "Nenhum item configurado!\n\n"
            "Adicione seus IDs de item no .env:\n"
            "  PLUGGY_ITEM_IDS=<uuid-xp>,<uuid-itau>\n\n"
            "Para obter os IDs:\n"
            "  1. Acesse dashboard.pluggy.ai → sua aplicação → Demo\n"
            "  2. Clique em 'Connect Account' e conecte XP e Itaú\n"
            "  3. Os IDs aparecerão na lista 'Connected Items'"
        )

    log(f"Buscando {len(ITEM_IDS)} item(s) configurado(s)...")
    items = []
    for item_id in ITEM_IDS:
        item = pluggy_get_item(token, item_id)
        nome = item.get("connector", {}).get("name", "?")
        status = item.get("status", "?")
        log(f"  📌 {nome} — status: {status} — id: {item['id']}")
        items.append(item)
    return items


def listar_contas(token, item_id):
    """Lista contas de um item (corrente, cartão, etc.)."""
    contas = pluggy_get(token, "/accounts", {"itemId": item_id})
    return contas


def buscar_transacoes(token, account_id, data_inicio, data_fim):
    """Busca transações de uma conta num período."""
    return pluggy_get(token, "/transactions", {
        "accountId": account_id,
        "from": data_inicio,
        "to": data_fim,
    })


def buscar_faturas(token, account_id):
    """Busca faturas de cartão de crédito."""
    return pluggy_get(token, "/bills", {"accountId": account_id})


# ══════════════════════════════════════
# CATEGORIZAÇÃO
# ══════════════════════════════════════

def categorizar(descricao):
    """Categoriza uma transação pela descrição."""
    desc_lower = descricao.lower()
    for nome, cfg in CATEGORIAS.items():
        for kw in cfg["keywords"]:
            if kw in desc_lower:
                return cfg["cat"]
    return "outros"


def processar_transacoes(transacoes):
    """Agrupa transações por mês e categoria."""
    meses = defaultdict(lambda: defaultdict(float))

    for tx in transacoes:
        valor = abs(tx.get("amount", 0))
        data = tx.get("date", "")[:10]  # YYYY-MM-DD
        descricao = tx.get("description", "")

        if not data or valor == 0:
            continue

        # Chave do mês: "2026-01" etc.
        mes_key = data[:7]
        cat = categorizar(descricao)
        meses[mes_key][cat] += valor

    return dict(meses)


# ══════════════════════════════════════
# GERAÇÃO DOS DADOS DO DASHBOARD
# ══════════════════════════════════════

MESES_PT = {
    "01": "jan", "02": "fev", "03": "mar", "04": "abr",
    "05": "mai", "06": "jun", "07": "jul", "08": "ago",
    "09": "set", "10": "out", "11": "nov", "12": "dez",
}

MESES_LABEL = {
    "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
    "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
    "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro",
}

# Fixos fora do XP (não vêm do cartão XP)
FIXOS_FORA_XP = 6032


def formatar_valor(v):
    """Formata número para padrão brasileiro: 1.234"""
    return f"{v:,.0f}".replace(",", ".")


def gerar_dados_mes(mes_key, dados_mes, fatura_total=None):
    """Gera o objeto de dados de um mês para o JS."""
    uber = dados_mes.get("uber", 0)
    xp_total = fatura_total or sum(dados_mes.values())
    gasto_total = xp_total + FIXOS_FORA_XP
    xp_pct = round(xp_total / gasto_total * 100) if gasto_total else 0

    ano = mes_key[:4]
    mes_num = mes_key[5:7]
    mes_curto = MESES_PT.get(mes_num, mes_num)
    mes_label = MESES_LABEL.get(mes_num, mes_num)

    # Tokio Marine por mês (estimativa baseada nos dados conhecidos)
    tokio = dados_mes.get("carro", 0)
    carro_total = 1510 + min(tokio, 700)  # parcela + seguro (cap razoável)

    # Determinar classe visual pelo gasto total
    if gasto_total > 15000:
        gasto_css, gasto_bcss = "bad", "down"
    elif gasto_total > 13000:
        gasto_css, gasto_bcss = "warn", "warn"
    else:
        gasto_css, gasto_bcss = "ok", "up"

    # Uber classe
    if uber > 1000:
        uber_css, uber_bcss = "bad", "down"
    elif uber > 700:
        uber_css, uber_bcss = "warn", "warn"
    else:
        uber_css, uber_bcss = "ok", "up"

    return {
        "gasto": {
            "val": formatar_valor(gasto_total),
            "css": gasto_css,
            "badge": f"XP + fixos R${formatar_valor(FIXOS_FORA_XP)}",
            "bcss": gasto_bcss,
            "sub": f"XP R${formatar_valor(xp_total)} + fixos R${formatar_valor(FIXOS_FORA_XP)}",
        },
        "xp": {
            "label": f"Cartão XP {mes_curto}/{ano[2:]}",
            "val": formatar_valor(xp_total),
            "css": "bad" if xp_total > 8000 else ("warn" if xp_total > 6500 else "ok"),
            "badge": f"{xp_pct}% do gasto",
            "bcss": "down" if xp_pct > 55 else ("warn" if xp_pct > 45 else "up"),
            "sub": f"fatura {mes_label.lower()} {ano}",
        },
        "carro": {
            "val": formatar_valor(carro_total),
            "badge": f"{round(carro_total/gasto_total*100)}% do total",
            "sub": f"R$1.510 parcela + R${formatar_valor(min(tokio, 700))} Tokio",
        },
        "uber": {
            "val": formatar_valor(uber),
            "css": uber_css,
            "badge": f"R$ {formatar_valor(uber)}/mês",
            "bcss": uber_bcss,
            "sub": f"Uber/99 em {mes_label.lower()}",
        },
        "fatura": {
            "label": f"Fatura XP {mes_curto}/{ano[2:]}",
            "val": formatar_valor(xp_total),
            "css": "ok" if xp_total < 7000 else ("warn" if xp_total < 9000 else "bad"),
            "badge": f"R$ {formatar_valor(xp_total)}",
            "bcss": "up" if xp_total < 7000 else ("warn" if xp_total < 9000 else "down"),
            "sub": f"fatura {mes_label.lower()} {ano}",
        },
    }


def gerar_dados_media(meses_dados):
    """Gera o objeto 'todos' (média) a partir dos meses."""
    if not meses_dados:
        return None

    n = len(meses_dados)
    totais_xp = []
    totais_uber = []
    totais_gasto = []

    for mes_key, dados in sorted(meses_dados.items()):
        xp = sum(dados.values())
        totais_xp.append(xp)
        totais_uber.append(dados.get("uber", 0))
        totais_gasto.append(xp + FIXOS_FORA_XP)

    avg_xp = sum(totais_xp) / n
    avg_uber = sum(totais_uber) / n
    avg_gasto = sum(totais_gasto) / n
    xp_pct = round(avg_xp / avg_gasto * 100) if avg_gasto else 0

    return {
        "gasto": {
            "val": formatar_valor(avg_gasto),
            "css": "bad" if avg_gasto > 15000 else ("warn" if avg_gasto > 13000 else "ok"),
            "badge": "⚠ Alto comprometimento" if avg_gasto > 14000 else "média mensal",
            "bcss": "down" if avg_gasto > 14000 else "warn",
            "sub": "fixos + XP + Itaú + outros",
        },
        "xp": {
            "label": f"Cartão XP (média {n} meses)",
            "val": formatar_valor(avg_xp),
            "css": "warn",
            "badge": f"{xp_pct}% do gasto total",
            "bcss": "warn",
            "sub": f"média de {n} meses",
        },
        "carro": {
            "val": "2.010",
            "badge": "12% do total",
            "sub": "R$1.510 parcela + R$500 Tokio",
        },
        "uber": {
            "val": formatar_valor(avg_uber),
            "css": "warn" if avg_uber > 700 else "ok",
            "badge": f"~{round(avg_uber/37)} corridas/mês",
            "bcss": "warn" if avg_uber > 700 else "up",
            "sub": f"média {n} meses — maior variável",
        },
        "fatura": {
            "label": "Fatura XP (média)",
            "val": formatar_valor(avg_xp),
            "css": "warn",
            "badge": f"média {n} faturas",
            "bcss": "warn",
            "sub": f"média de {n} meses",
        },
    }


def gerar_opcoes_select(meses_dados):
    """Gera o HTML do <select> com os meses disponíveis."""
    opcoes = ['        <option value="todos">Todos (média)</option>']
    for mes_key in sorted(meses_dados.keys()):
        mes_num = mes_key[5:7]
        ano = mes_key[:4]
        label = MESES_LABEL.get(mes_num, mes_num)
        val = MESES_PT.get(mes_num, mes_num)
        opcoes.append(f'        <option value="{val}">{label} {ano}</option>')
    return "\n".join(opcoes)


def gerar_js_md(meses_dados):
    """Gera o bloco JS do objeto MD com dados reais."""
    md = {}

    # Média
    media = gerar_dados_media(meses_dados)
    if media:
        md["todos"] = media

    # Por mês
    for mes_key in sorted(meses_dados.keys()):
        mes_num = mes_key[5:7]
        val_key = MESES_PT.get(mes_num, mes_num)
        md[val_key] = gerar_dados_mes(mes_key, meses_dados[mes_key])

    # Serializar para JS
    return "const MD = " + json.dumps(md, ensure_ascii=False, indent=2) + ";"


# ══════════════════════════════════════
# ATUALIZAÇÃO DO HTML
# ══════════════════════════════════════

def atualizar_html(meses_dados):
    """Atualiza o index.html com os novos dados."""
    log("Atualizando index.html...")

    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. Atualizar as opções do select de meses
    novo_select = gerar_opcoes_select(meses_dados)
    html = re.sub(
        r'(<select[^>]*id="monthFilter"[^>]*>)\s*(.*?)\s*(</select>)',
        lambda m: m.group(1) + "\n" + novo_select + "\n      " + m.group(3),
        html,
        flags=re.DOTALL,
    )

    # 2. Atualizar o objeto MD no JS
    novo_md = gerar_js_md(meses_dados)
    html = re.sub(
        r'const MD = \{.*?\};',
        novo_md,
        html,
        flags=re.DOTALL,
    )

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    log("index.html atualizado com sucesso!")


# ══════════════════════════════════════
# MAIN
# ══════════════════════════════════════

def main():
    print("\n🔄 Atualizando Dashboard Financeiro\n")

    # 1. Autenticar
    token = pluggy_auth()

    # 2. Listar conexões
    items = listar_items(token)
    if not items:
        erro("Nenhuma conexão encontrada. Conecte suas contas no dashboard do Pluggy.")

    # 3. Para cada item, buscar contas e transações
    # Período: últimos 6 meses
    hoje = datetime.now()
    data_inicio = (hoje - timedelta(days=180)).strftime("%Y-%m-%d")
    data_fim = hoje.strftime("%Y-%m-%d")

    todas_transacoes = []

    for item in items:
        nome = item.get("connector", {}).get("name", "?")
        log(f"Buscando contas de {nome}...")

        contas = listar_contas(token, item["id"])
        for conta in contas:
            tipo = conta.get("type", "")
            subtipo = conta.get("subtype", "")
            conta_nome = conta.get("name", tipo)
            log(f"  💳 {conta_nome} ({tipo}/{subtipo})")

            # Buscar transações
            txs = buscar_transacoes(token, conta["id"], data_inicio, data_fim)
            log(f"     {len(txs)} transações encontradas")
            todas_transacoes.extend(txs)

            # Se for cartão de crédito, buscar faturas também
            if tipo.upper() == "CREDIT" or subtipo == "CREDIT_CARD":
                faturas = buscar_faturas(token, conta["id"])
                log(f"     {len(faturas)} faturas encontradas")

    log(f"\nTotal: {len(todas_transacoes)} transações")

    # 4. Processar e categorizar
    log("Categorizando transações...")
    meses_dados = processar_transacoes(todas_transacoes)

    if not meses_dados:
        erro("Nenhuma transação encontrada no período.")

    # Mostrar resumo
    print("\n📊 Resumo por mês:\n")
    for mes in sorted(meses_dados.keys()):
        total = sum(meses_dados[mes].values())
        cats = ", ".join(f"{k}=R${v:,.0f}" for k, v in sorted(meses_dados[mes].items(), key=lambda x: -x[1]))
        print(f"  {mes}: R$ {total:,.0f}")
        print(f"         {cats}\n")

    # 5. Atualizar HTML
    atualizar_html(meses_dados)

    print("\n✅ Dashboard atualizado! Faça push para publicar:")
    print("   git add index.html && git commit -m 'Atualiza dados via Pluggy' && git push\n")


if __name__ == "__main__":
    main()
