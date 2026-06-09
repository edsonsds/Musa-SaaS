#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Musa SaaS — Gestão para Salões (Multi-Tenant)
Arquitetura: Flask + PostgreSQL (Neon)
Cada salão tem salon_id isolado em todas as tabelas operacionais.
Super-admin gerencia cadastro de salões via /admin
"""

from flask import Flask, jsonify, request, send_from_directory, session, g, redirect, url_for
try:
    from ia_wpp import responder as ia_responder, detectar_intencao
    IA_DISPONIVEL = True
except ImportError:
    IA_DISPONIVEL = False
import os, json, datetime, re, hashlib, psycopg2, psycopg2.extras

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB para importações grandes
app.secret_key = os.environ.get('MUSA_SECRET_KEY', 'musa_saas_chave_fixa_producao_2024_nao_alterar_xK9mP2vL7qR4')
# Sessão dura 30 dias e persiste mesmo fechando o navegador
import datetime as _dt_sess
app.permanent_session_lifetime = _dt_sess.timedelta(days=30)

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── CONNECTION POOL ─────────────────────────────────────────────────────────
from psycopg2 import pool as pg_pool

_pool = None

def get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _pool

def get_db():
    if 'db' not in g:
        try:
            conn = get_pool().getconn()
            conn.autocommit = False
            g.db = conn
        except Exception:
            # Fallback: conexão direta
            conn = psycopg2.connect(DATABASE_URL)
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            g.db = conn
            g.db_direct = True
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        if g.pop('db_direct', False):
            db.close()
        else:
            try:
                get_pool().putconn(db)
            except Exception:
                db.close()

def db_exec(sql, params=(), fetch='none'):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(sql, params)
    if fetch == 'one':  return cur.fetchone()
    if fetch == 'all':  return cur.fetchall()
    if fetch == 'id':   return cur.fetchone()[0] if cur.rowcount else None
    return cur

def db_commit():
    get_db().commit()

def db_rollback():
    try:
        get_db().rollback()
    except Exception:
        pass

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def hash_senha(s):
    return hashlib.sha256(s.encode()).hexdigest()

_BR_OFFSET = datetime.timedelta(hours=-3)
def now_br():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + _BR_OFFSET
def today_br():
    return now_br().date()

# ─── TENANT CONTEXT ───────────────────────────────────────────────────────────
# Todas as rotas /api/* usam g.salon_id para filtrar dados.
# O salon_id vem da sessão do usuário logado.

def get_salon_id():
    """Retorna salon_id da sessão ou None se não logado."""
    return session.get('salon_id')

def require_salon():
    """Decorator helper — retorna (salon_id, None) ou (None, response_403)."""
    sid = get_salon_id()
    if not sid:
        return None, (jsonify({'erro': 'Não autenticado'}), 401)
    return sid, None

def require_admin():
    """Exige perfil admin dentro do salão."""
    sid = get_salon_id()
    if not sid:
        return None, (jsonify({'erro': 'Não autenticado'}), 401)
    if session.get('uperfil') != 'admin':
        return None, (jsonify({'erro': 'Sem permissão'}), 403)
    return sid, None

# ─── INIT DB ──────────────────────────────────────────────────────────────────
def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    # ── Tabela de salões (super-admin) ────────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS saloes (
        id SERIAL PRIMARY KEY,
        nome TEXT NOT NULL,
        telefone TEXT DEFAULT '',
        endereco TEXT DEFAULT '',
        logo TEXT DEFAULT '',
        email TEXT DEFAULT '',
        plano TEXT DEFAULT 'trial',
        ativo INTEGER DEFAULT 1,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS super_admins (
        id SERIAL PRIMARY KEY,
        login TEXT NOT NULL UNIQUE,
        senha_hash TEXT NOT NULL,
        nome TEXT DEFAULT 'Super Admin'
    )""")

    # Seed super admin
    cur.execute("SELECT COUNT(*) FROM super_admins")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO super_admins (login,senha_hash,nome) VALUES (%s,%s,%s)",
                    ('superadmin', hash_senha('musa2024'), 'Super Admin'))

    # ── Tabelas operacionais (todas com salon_id) ─────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sistema_config (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        chave TEXT NOT NULL,
        valor TEXT DEFAULT '',
        UNIQUE(salon_id, chave)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS profissionais (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        cargo TEXT DEFAULT '',
        cor TEXT DEFAULT '#EC4899',
        comissao_pct REAL DEFAULT 40,
        h_inicio TEXT DEFAULT '08:00',
        h_fim TEXT DEFAULT '20:00',
        foto_base64 TEXT DEFAULT '',
        ativo INTEGER DEFAULT 1,
        categorias TEXT DEFAULT '',
        email TEXT DEFAULT '',
        senha_hash TEXT DEFAULT '',
        pode_login INTEGER DEFAULT 0,
        pode_ver_comissao INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS servicos (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        categoria TEXT DEFAULT '',
        duracao_min INTEGER DEFAULT 60,
        preco REAL DEFAULT 0,
        comissao_pct REAL DEFAULT 40,
        ativo INTEGER DEFAULT 1,
        alerta_retorno_dias INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pro_svc_config (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pro_id INTEGER NOT NULL,
        categoria TEXT NOT NULL DEFAULT '',
        svc_id INTEGER DEFAULT 0,
        comissao_override REAL DEFAULT -1,
        UNIQUE(salon_id, pro_id, svc_id)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        tel TEXT DEFAULT '',
        email TEXT DEFAULT '',
        cpf TEXT DEFAULT '',
        nasc TEXT DEFAULT '',
        obs TEXT DEFAULT '',
        pro_fav INTEGER DEFAULT 0,
        svc_fav INTEGER DEFAULT 0,
        ultima_visita TEXT DEFAULT '',
        ativo INTEGER DEFAULT 1,
        criado_em TEXT DEFAULT ''
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS agendamentos (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER,
        pro_id INTEGER,
        svc_id INTEGER,
        data TEXT NOT NULL,
        h_ini TEXT NOT NULL,
        h_fim TEXT NOT NULL,
        preco REAL DEFAULT 0,
        status TEXT DEFAULT 'agendado',
        pag TEXT DEFAULT '',
        gorjeta REAL DEFAULT 0,
        obs TEXT DEFAULT '',
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS caixa (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        tipo TEXT NOT NULL,
        descricao TEXT DEFAULT '',
        valor REAL DEFAULT 0,
        pag TEXT DEFAULT '',
        ag_id INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS despesas (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        descricao TEXT NOT NULL,
        valor REAL DEFAULT 0,
        categoria TEXT DEFAULT '',
        pago INTEGER DEFAULT 0,
        vencimento TEXT DEFAULT '',
        data_pagamento TEXT DEFAULT ''
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS comissoes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pro_id INTEGER,
        ag_id INTEGER,
        valor REAL DEFAULT 0,
        pago INTEGER DEFAULT 0,
        data_pagamento TEXT DEFAULT '',
        com_pct REAL DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS estoque (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        categoria TEXT DEFAULT '',
        quantidade REAL DEFAULT 0,
        minimo REAL DEFAULT 0,
        unidade TEXT DEFAULT 'un'
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS taxas (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pag TEXT NOT NULL,
        taxa_pct REAL DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS indisponibilidades (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pro_id INTEGER,
        data_inicio TEXT,
        data_fim TEXT,
        motivo TEXT DEFAULT ''
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ocorrencias (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER,
        pro_id INTEGER,
        data TEXT,
        tipo TEXT DEFAULT '',
        descricao TEXT DEFAULT '',
        resolvido INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS retornos (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER,
        ag_id INTEGER,
        data_retorno TEXT,
        motivo TEXT DEFAULT '',
        realizado INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS avaliacoes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER,
        ag_id INTEGER,
        nota INTEGER DEFAULT 5,
        comentario TEXT DEFAULT '',
        data TEXT
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS varejo (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        data TEXT,
        cli_id INTEGER DEFAULT 0,
        produto TEXT,
        quantidade REAL DEFAULT 1,
        preco_unit REAL DEFAULT 0,
        total REAL DEFAULT 0,
        pag TEXT DEFAULT ''
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS agenda_financeira (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        data TEXT,
        descricao TEXT,
        valor REAL DEFAULT 0,
        tipo TEXT DEFAULT 'receita',
        realizado INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS metas (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pro_id INTEGER NOT NULL,
        mes TEXT NOT NULL,
        valor_meta REAL DEFAULT 0,
        UNIQUE(salon_id, pro_id, mes)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS fila_espera (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER,
        pro_id INTEGER DEFAULT 0,
        svc_id INTEGER DEFAULT 0,
        data_preferencia TEXT DEFAULT '',
        obs TEXT DEFAULT '',
        atendido INTEGER DEFAULT 0,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS historico_agendamentos (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        ag_id INTEGER,
        campo TEXT,
        valor_antes TEXT,
        valor_depois TEXT,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS retorno_alertas (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        cli_id INTEGER NOT NULL,
        ag_id INTEGER DEFAULT 0,
        svc_id INTEGER DEFAULT 0,
        svc_nome TEXT DEFAULT '',
        data_atendimento TEXT,
        data_retorno TEXT,
        dias INTEGER DEFAULT 30,
        notificado INTEGER DEFAULT 0,
        realizado INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS lixeira_agendamentos (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        ag_id INTEGER,
        cli_nome TEXT,
        pro_nome TEXT,
        svc_nome TEXT,
        data TEXT,
        h_ini TEXT,
        h_fim TEXT,
        preco REAL DEFAULT 0,
        status TEXT,
        pag TEXT,
        obs TEXT,
        excluido_por TEXT DEFAULT 'admin',
        excluido_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS comissoes_varejo (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        pro_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        produto TEXT NOT NULL,
        cli_nome TEXT DEFAULT '',
        valor_venda REAL DEFAULT 0,
        com_pct REAL DEFAULT 0,
        com_valor REAL DEFAULT 0,
        pago INTEGER DEFAULT 0,
        data_pagamento TEXT DEFAULT '',
        obs TEXT DEFAULT ''
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_templates (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        tipo TEXT DEFAULT 'aviso',
        mensagem TEXT NOT NULL,
        ativo INTEGER DEFAULT 1,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_envios (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        template_id INTEGER DEFAULT 0,
        template_nome TEXT DEFAULT '',
        cli_nome TEXT DEFAULT '',
        cli_tel TEXT DEFAULT '',
        ag_id INTEGER DEFAULT 0,
        mensagem TEXT DEFAULT '',
        enviado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT NOT NULL,
        login TEXT NOT NULL,
        senha_hash TEXT NOT NULL,
        perfil TEXT DEFAULT 'recepcionista',
        permissoes TEXT DEFAULT '[]',
        ativo INTEGER DEFAULT 1,
        criado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE(salon_id, login)
    )""")
    # Migração: coluna telefone para login alternativo
    try:
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS telefone TEXT DEFAULT ''")
    except Exception:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pacotes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT,
        descricao TEXT DEFAULT '',
        preco REAL DEFAULT 0,
        validade_dias INTEGER DEFAULT 30
    )""")


    cur.execute("""
    CREATE TABLE IF NOT EXISTS sistema_global (
        id SERIAL PRIMARY KEY,
        chave TEXT NOT NULL UNIQUE,
        valor TEXT DEFAULT '',
        atualizado_em TIMESTAMP DEFAULT NOW()
    )""")

    # Seed config Evolution API
    cur.execute("""
        INSERT INTO sistema_global (chave, valor)
        VALUES ('evolution_url', ''), ('evolution_apikey', ''), ('cron_key', 'musa_cron_2024')
        ON CONFLICT (chave) DO NOTHING
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_conexoes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL UNIQUE,
        numero TEXT DEFAULT '',
        instance_name TEXT DEFAULT '',
        instance_key TEXT DEFAULT '',
        evolution_url TEXT DEFAULT '',
        ativo INTEGER DEFAULT 0,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_ia_config (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL UNIQUE,
        ativo INTEGER DEFAULT 0,
        groq_key TEXT DEFAULT '',
        saudacao TEXT DEFAULT '',
        horario_ini TEXT DEFAULT '08:00',
        horario_fim TEXT DEFAULT '20:00',
        dias_semana TEXT DEFAULT '1,2,3,4,5,6',
        msg_fora_horario TEXT DEFAULT 'Olá! No momento estamos fechados. Retornaremos em breve! 😊',
        personalidade TEXT DEFAULT '',
        atualizado_em TIMESTAMP DEFAULT NOW()
    )""")
    # Migração: adicionar coluna personalidade se não existir
    try:
        cur.execute("ALTER TABLE wpp_ia_config ADD COLUMN IF NOT EXISTS personalidade TEXT DEFAULT ''")
    except Exception:
        pass
    # Migração: modo de atendimento ('24h', 'fora_expediente', 'desativado')
    try:
        cur.execute("ALTER TABLE wpp_ia_config ADD COLUMN IF NOT EXISTS modo_atendimento TEXT DEFAULT '24h'")
    except Exception:
        pass

    # Configuração de contato automático (Retorno e Inativos) — independente da IA
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contato_auto_config (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        ativo INTEGER DEFAULT 0,
        modo TEXT DEFAULT 'fixo',
        msg_fixa TEXT DEFAULT '',
        horario TEXT DEFAULT '08:00',
        dias_semana TEXT DEFAULT '1,2,3,4,5',
        dias_inativo INTEGER DEFAULT 40,
        dias_antes INTEGER DEFAULT 7,
        ultimo_envio DATE,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE(salon_id, tipo)
    )""")
    # Registro de quem já recebeu (evita mandar 2x para o mesmo cliente)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contato_auto_log (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        cli_id INTEGER,
        numero TEXT,
        enviado_em TIMESTAMP DEFAULT NOW()
    )""")
    try:
        cur.execute("ALTER TABLE contato_auto_config ADD COLUMN IF NOT EXISTS dias_antes INTEGER DEFAULT 7")
    except Exception:
        pass
    # Campos para lembrete de agendamento (antecedências on/off)
    for col, default in [('lemb_1dia','1'), ('lemb_2h','0'), ('lemb_1h','0'), ('confirma_palavra',"'1'")]:
        try:
            cur.execute("ALTER TABLE contato_auto_config ADD COLUMN IF NOT EXISTS %s TEXT DEFAULT %s" % (col, default))
        except Exception:
            pass
    # Registro de lembretes já enviados (por agendamento + antecedência)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lembrete_log (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        ag_id INTEGER NOT NULL,
        antecedencia TEXT NOT NULL,
        enviado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE(ag_id, antecedencia)
    )""")

    # Campanhas de disparo em lote (responsável: espaçado + limite diário)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS disparo_campanha (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        titulo TEXT DEFAULT '',
        mensagem TEXT NOT NULL,
        total INTEGER DEFAULT 0,
        enviados INTEGER DEFAULT 0,
        falhas INTEGER DEFAULT 0,
        status TEXT DEFAULT 'rodando',
        limite_dia INTEGER DEFAULT 80,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS disparo_log (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        campanha_id INTEGER NOT NULL,
        cli_id INTEGER,
        numero TEXT DEFAULT '',
        status TEXT DEFAULT 'pendente',
        enviado_em TIMESTAMP,
        UNIQUE(campanha_id, cli_id)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_conversas (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        numero_cliente TEXT NOT NULL,
        nome_cliente TEXT DEFAULT '',
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        criado_em TIMESTAMP DEFAULT NOW()
    )""")

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_wpp_conv ON wpp_conversas(salon_id, numero_cliente, criado_em)
    """)

    # Controle de pausa da IA por conversa (handoff para humano)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS wpp_ia_pausa (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        numero_cliente TEXT NOT NULL,
        pausado_ate TIMESTAMP,
        pausado_manual INTEGER DEFAULT 0,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE(salon_id, numero_cliente)
    )""")

    indices = [
        "CREATE INDEX IF NOT EXISTS idx_ag_salon_data ON agendamentos(salon_id, data)",
        "CREATE INDEX IF NOT EXISTS idx_ag_salon_status ON agendamentos(salon_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_ag_salon_data_status ON agendamentos(salon_id, data, status)",
        "CREATE INDEX IF NOT EXISTS idx_ag_pro ON agendamentos(salon_id, pro_id)",
        "CREATE INDEX IF NOT EXISTS idx_ag_cli ON agendamentos(salon_id, cli_id)",
        "CREATE INDEX IF NOT EXISTS idx_caixa_salon_data ON caixa(salon_id, data)",
        "CREATE INDEX IF NOT EXISTS idx_cli_salon ON clientes(salon_id, ativo)",
        "CREATE INDEX IF NOT EXISTS idx_cli_salon_criado ON clientes(salon_id, criado_em)",
        "CREATE INDEX IF NOT EXISTS idx_com_salon ON comissoes(salon_id, pro_id)",
        "CREATE INDEX IF NOT EXISTS idx_ret_salon ON retorno_alertas(salon_id, realizado, data_retorno)",
    ]
    for idx_sql in indices:
        try: cur.execute(idx_sql)
        except: pass
    conn.commit()
    cur.close()
    conn.close()
    print("Banco inicializado com sucesso.")

# ─── SEED NOVO SALÃO ─────────────────────────────────────────────────────────
def seed_novo_salao(salon_id, nome_salao='Meu Salão'):
    """Popula taxas, templates WhatsApp e usuário admin para um novo salão."""
    # Taxas
    r = db_exec("SELECT COUNT(*) as n FROM taxas WHERE salon_id=%s", (salon_id,), 'one')
    if r['n'] == 0:
        taxas = [('Dinheiro',0),('PIX',0),('Débito',1.5),('Crédito à vista',2.99),
                 ('Crédito 2x',3.99),('Crédito 3x',4.99),('Transferência',0)]
        for pag, pct in taxas:
            db_exec("INSERT INTO taxas (salon_id,pag,taxa_pct) VALUES (%s,%s,%s)", (salon_id,pag,pct))

    # WhatsApp templates
    r = db_exec("SELECT COUNT(*) as n FROM wpp_templates WHERE salon_id=%s", (salon_id,), 'one')
    if r['n'] == 0:
        tpls = [
            ('✅ Confirmação de Agendamento','confirmacao',
             'Olá, {nome}! 😊\n\n✅ *Seu agendamento está confirmado!*\n\n📅 *Data:* {dia_semana}, {data}\n🕐 *Horário:* {hora_ini} às {hora_fim}\n✂️ *Serviço:* {servico}\n👩 *Profissional:* {profissional}\n💰 *Valor:* {valor}\n\nTe esperamos! 💕\n— {salao}'),
            ('🔔 Lembrete de Agendamento','lembrete',
             'Olá, {nome}! 😊\n\n🔔 *Lembrete de Agendamento*\n\n📅 *Data:* {dia_semana}, {data}\n🕐 *Horário:* {hora_ini} às {hora_fim}\n✂️ *Serviço:* {servico}\n👩 *Profissional:* {profissional}\n\nTe esperamos! 💕\n— {salao}'),
            ('🎉 Parabéns Aniversário','aniversario',
             'Olá, {nome}! 🎉\n\nToda a equipe do {salao} deseja um *Feliz Aniversário*! 🥳\n\nVenha comemorar com a gente! 💕'),
            ('💬 Aviso Geral','aviso',
             'Olá, {nome}! 😊\n\nPassando para dar um aviso importante do {salao}.\n\n_Escreva sua mensagem aqui..._\n\nQualquer dúvida, estamos à disposição! 💕'),
            ('🔁 Alerta de Retorno','retorno',
             'Oi, {nome}! 💕\n\nAqui é do {salao} — passando para lembrar que está chegando a hora do seu retorno! ✨\n\n✂️ *Serviço:* {servico}\n📅 *Retorno previsto:* {data}\n\nQuer reservar um horário? 🥰\n— Equipe {salao}'),
            ('💔 Reativação Cliente Inativo','reativacao',
             'Oi, {nome}! 💕\n\nSentimos sua falta no {salao}! 🥺\n\n✨ Preparamos uma condição especial só pra te trazer de volta!\n\n📲 Responda com o melhor dia e horário pra você!\n— Equipe {salao}'),
        ]
        for nome,tipo,msg in tpls:
            db_exec("INSERT INTO wpp_templates (salon_id,nome,tipo,mensagem) VALUES (%s,%s,%s,%s)",
                    (salon_id,nome,tipo,msg))

    # Usuario admin do salão
    r = db_exec("SELECT COUNT(*) as n FROM usuarios WHERE salon_id=%s AND login='admin'", (salon_id,), 'one')
    if r['n'] == 0:
        db_exec("INSERT INTO usuarios (salon_id,nome,login,senha_hash,perfil,permissoes) VALUES (%s,%s,%s,%s,%s,%s)",
                (salon_id,'Administrador','admin',hash_senha('admin'),'admin','[]'))

    # Config admin
    for chave, valor in [('usuario_admin','admin'),('senha_admin',hash_senha('admin')),('email_recuperacao','')]:
        db_exec("""INSERT INTO sistema_config (salon_id,chave,valor) VALUES (%s,%s,%s)
                   ON CONFLICT (salon_id,chave) DO NOTHING""", (salon_id,chave,valor))

    # ─── SERVIÇOS DE EXEMPLO ───
    r = db_exec("SELECT COUNT(*) as n FROM servicos WHERE salon_id=%s", (salon_id,), 'one')
    if r['n'] == 0:
        servicos = [
            # (nome, categoria, preco, duracao_min, comissao)
            ('Progressiva','Cabelo',250.0,120,40.0),
            ('Tratamento Cetim','Cabelo',120.0,40,40.0),
            ('Escova','Cabelo',70.0,45,50.0),
            ('Escova Babyliss','Cabelo',90.0,60,0.0),
            ('Luzes','Cabelo',400.0,180,40.0),
            ('Penteado','Cabelo',240.0,60,40.0),
            ('Corte','Cabelo',80.0,30,40.0),
            ('Tratamento Alquimia','Cabelo',160.0,40,40.0),
            ('Terapia Capilar','Cabelo',250.0,60,40.0),
            ('Botox','Cabelo',190.0,120,40.0),
            ('Corte Masculino','Cabelo',60.0,40,50.0),
            ('Tratamento Terapeutico','Cabelo',190.0,60,40.0),
            ('Avaliação Terapia','Cabelo',0.0,30,0.0),
            ('Alinhamento Ortomolecular','Cabelo',270.0,120,40.0),
            ('Alinhamento Orgânico','Cabelo',280.0,180,40.0),
            ('Soltura de Cachos','Cabelo',260.0,120,0.0),
            ('Mão','Manicure',35.0,30,50.0),
            ('PÉ','Manicure',40.0,30,50.0),
            ('Pé e Mão','Manicure',70.0,60,50.0),
            ('Spa dos Pés','Manicure',50.0,45,40.0),
            ('Esmaltação','Manicure',20.0,30,50.0),
            ('Blindagem Mão','Manicure',40.0,30,50.0),
            ('Plastica dos Pés','Manicure',90.0,60,40.0),
            ('Mão Masculina','Manicure',37.0,40,50.0),
            ('Axilas','Depilação',25.0,30,40.0),
            ('Depilação Feminina Completa','Depilação',120.0,60,40.0),
            ('Depilação Feminina Rosto','Depilação',42.0,30,0.0),
            ('Depilação Intima','Depilação',70.0,40,40.0),
            ('Depilação Meia Perna','Depilação',30.0,30,40.0),
            ('Depilação Perna Inteira','Depilação',60.0,45,40.0),
            ('Depilação Virilha Cavada','Depilação',35.0,40,40.0),
            ('Depilação Virilha Completa','Depilação',50.0,40,40.0),
            ('Buço','Depilação',30.0,15,40.0),
            ('Limpeza Sobrancelha','Estética Facial',40.0,30,50.0),
            ('Sobrancelha Henna','Estética Facial',70.0,60,40.0),
            ('Maquiagem','Estética Facial',290.0,120,40.0),
            ('Cilios','Estética Facial',150.0,120,40.0),
            ('Terapia Sobrancelha','Estética Facial',120.0,30,40.0),
            ('Aplicação Coloração','Coloração',99.0,40,40.0),
            ('Coloração','Coloração',130.0,50,40.0),
            ('Barba','Barba',40.0,20,40.0),
        ]
        for nome,cat,preco,dur,com in servicos:
            db_exec("""INSERT INTO servicos (salon_id,nome,categoria,preco,duracao_min,comissao_pct,ativo)
                       VALUES (%s,%s,%s,%s,%s,%s,1)""", (salon_id,nome,cat,preco,dur,com))

    # ─── PROFISSIONAIS DE EXEMPLO ───
    r = db_exec("SELECT COUNT(*) as n FROM profissionais WHERE salon_id=%s", (salon_id,), 'one')
    if r['n'] == 0:
        pros = [
            ('Beatriz Costa','Cabeleireira','#ec4899',40),
            ('Mariana Santos','Manicure','#8b5cf6',50),
            ('Anelene Silva','Esteticista','#06b6d4',45),
        ]
        for nome,cargo,cor,com in pros:
            db_exec("""INSERT INTO profissionais (salon_id,nome,cargo,cor,comissao_pct,h_inicio,h_fim,ativo)
                       VALUES (%s,%s,%s,%s,%s,'09:00','19:00',1)""", (salon_id,nome,cargo,cor,com))

    # ─── CLIENTES E AGENDAMENTOS DE EXEMPLO ───
    r = db_exec("SELECT COUNT(*) as n FROM clientes WHERE salon_id=%s", (salon_id,), 'one')
    if r['n'] == 0:
        import datetime as _dt
        clientes_ex = [
            ('Ana Paula Oliveira','11987654321'),
            ('Carla Mendes','11976543210'),
            ('Juliana Ferreira','11965432109'),
        ]
        cli_ids = []
        for nome,tel in clientes_ex:
            cur = db_exec("""INSERT INTO clientes (salon_id,nome,tel,ativo)
                            VALUES (%s,%s,%s,1) RETURNING id""", (salon_id,nome,tel), 'one')
            cli_ids.append(cur['id'])

        # Buscar IDs de profissionais e serviços recém-criados
        pros_r = db_exec("SELECT id FROM profissionais WHERE salon_id=%s ORDER BY id LIMIT 3", (salon_id,), 'all')
        svcs_r = db_exec("SELECT id,nome,preco,duracao_min FROM servicos WHERE salon_id=%s ORDER BY id LIMIT 5", (salon_id,), 'all')

        if pros_r and svcs_r:
            hoje = _dt.date.today()
            # Criar 3 agendamentos de exemplo para hoje e amanhã
            ags = [
                (cli_ids[0], pros_r[0]['id'], svcs_r[0], hoje, '10:00'),
                (cli_ids[1], pros_r[1]['id'] if len(pros_r)>1 else pros_r[0]['id'], svcs_r[2] if len(svcs_r)>2 else svcs_r[0], hoje, '14:00'),
                (cli_ids[2], pros_r[0]['id'], svcs_r[1] if len(svcs_r)>1 else svcs_r[0], hoje + _dt.timedelta(days=1), '11:00'),
            ]
            for cli_id, pro_id, svc, data_ag, hora in ags:
                dur = svc['duracao_min'] or 60
                h, m = int(hora.split(':')[0]), int(hora.split(':')[1])
                fim_min = h*60 + m + dur
                hora_fim = '%02d:%02d' % (fim_min//60, fim_min%60)
                db_exec("""INSERT INTO agendamentos
                           (salon_id,cli_id,pro_id,svc_id,data,h_ini,h_fim,status,preco)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'agendado',%s)""",
                        (salon_id, cli_id, pro_id, svc['id'], str(data_ag), hora, hora_fim, svc['preco']))

    db_commit()

# ─── PERMISSÕES ───────────────────────────────────────────────────────────────
ALL_TELAS = ['dashboard','agenda','clientes','profissionais','servicos','caixa',
             'comandas','comissoes','faturamento','despesas','agfin','retornos',
             'inativos','estoque','varejo','ocorrencias','avaliacoes','importar',
             'config','aniversariantes','metas','filaespera','exportar','lixeira','comvarejo','wpp_ia']
ALL_ACOES = ['criar_agendamento','editar_agendamento','excluir_agendamento',
             'fechar_comanda','editar_clientes','abrir_fechar_caixa']
PERFIS_PADRAO = {
    'admin':         ALL_TELAS + ALL_ACOES,
    'recepcionista': ['dashboard','agenda','clientes','caixa','comandas','retornos',
                      'aniversariantes','filaespera','avaliacoes','inativos',
                      'criar_agendamento','editar_agendamento','fechar_comanda',
                      'editar_clientes','abrir_fechar_caixa'],
    'profissional':  ['agenda','comandas','fechar_comanda'],
    'visualizador':  ['dashboard','agenda','clientes','faturamento'],
}
def get_permissoes(u):
    if u['perfil'] == 'admin':
        return ALL_TELAS + ALL_ACOES
    try:
        p = json.loads(u['permissoes'] or '[]')
        return p if isinstance(p, list) else [k for k,v in p.items() if v]
    except:
        return PERFIS_PADRAO.get(u['perfil'], [])

# ══════════════════════════════════════════════════════════════════════════════
# ROTAS SUPER-ADMIN (gestão de salões)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/superadmin')
@app.route('/superadmin/')
def superadmin_page():
    return send_from_directory('static', 'superadmin.html')

@app.route('/api/superadmin/login', methods=['POST'])
def superadmin_login():
    d = request.json or {}
    sa = db_exec("SELECT * FROM super_admins WHERE login=%s", (d.get('login',''),), 'one')
    if sa and sa['senha_hash'] == hash_senha(d.get('senha','')):
        session['sa_logado'] = True
        session['sa_nome']   = sa['nome']
        return jsonify({'ok': True, 'nome': sa['nome']})
    return jsonify({'ok': False, 'erro': 'Login ou senha inválidos'})

@app.route('/api/superadmin/logout', methods=['POST'])
def superadmin_logout():
    session.pop('sa_logado', None)
    session.pop('sa_nome', None)
    return jsonify({'ok': True})

@app.route('/api/superadmin/session', methods=['GET'])
def superadmin_session():
    return jsonify({'logado': bool(session.get('sa_logado')), 'nome': session.get('sa_nome','')})

def _require_sa():
    if not session.get('sa_logado'):
        return jsonify({'erro': 'Acesso negado'}), 403
    return None

@app.route('/api/superadmin/saloes', methods=['GET'])
def sa_saloes_list():
    err = _require_sa()
    if err: return err
    rows = db_exec("""
        SELECT s.*, 
               (SELECT COUNT(*) FROM usuarios WHERE salon_id=s.id) as total_usuarios,
               (SELECT COUNT(*) FROM clientes WHERE salon_id=s.id AND ativo=1) as total_clientes,
               (SELECT COUNT(*) FROM agendamentos WHERE salon_id=s.id) as total_ags
        FROM saloes s ORDER BY s.criado_em DESC
    """, fetch='all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/superadmin/saloes', methods=['POST'])
def sa_saloes_create():
    err = _require_sa()
    if err: return err
    d = request.json or {}
    if not d.get('nome'):
        return jsonify({'ok': False, 'erro': 'Nome do salão é obrigatório'})
    cur = db_exec("""INSERT INTO saloes (nome,telefone,endereco,email,plano,ativo)
                     VALUES (%s,%s,%s,%s,%s,1) RETURNING id""",
                  (d['nome'], d.get('telefone',''), d.get('endereco',''),
                   d.get('email',''), d.get('plano','trial')), 'one')
    salon_id = cur['id']
    db_commit()
    seed_novo_salao(salon_id, d['nome'])
    return jsonify({'ok': True, 'id': salon_id})

@app.route('/api/superadmin/saloes/<int:sid>', methods=['GET','PUT','DELETE'])
def sa_salao(sid):
    err = _require_sa()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT * FROM saloes WHERE id=%s", (sid,), 'one')
        return jsonify(dict(row) if row else {})
    if request.method == 'DELETE':
        db_exec("UPDATE saloes SET ativo=0 WHERE id=%s", (sid,))
        db_commit()
        return jsonify({'ok': True})
    d = request.json or {}
    db_exec("""UPDATE saloes SET nome=%s,telefone=%s,endereco=%s,email=%s,plano=%s,ativo=%s
               WHERE id=%s""",
            (d.get('nome'), d.get('telefone',''), d.get('endereco',''),
             d.get('email',''), d.get('plano','trial'), d.get('ativo',1), sid))
    db_commit()
    return jsonify({'ok': True})



@app.route('/api/superadmin/saloes/<int:sid>/deletar', methods=['DELETE'])
def sa_deletar_salao(sid):
    err = _require_sa()
    if err: return err
    # Deletar todos os dados do salão
    tabelas = ['agendamentos','clientes','profissionais','servicos','usuarios',
               'caixa','despesas','comissoes','estoque','taxas','wpp_templates',
               'wpp_envios','retorno_alertas','lixeira_agendamentos','ocorrencias',
               'avaliacoes','varejo','metas','fila_espera','indisponibilidades',
               'pro_svc_config','sistema_config','historico_agendamentos',
               'comissoes_varejo','agenda_financeira','pacotes','retornos']
    for t in tabelas:
        try:
            db_exec("DELETE FROM " + t + " WHERE salon_id=%s", (sid,))
        except: pass
    db_exec("DELETE FROM saloes WHERE id=%s", (sid,))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/superadmin/seed-todos', methods=['POST'])
def sa_seed_todos():
    err = _require_sa()
    if err: return err
    saloes = db_exec("SELECT * FROM saloes WHERE ativo=1", fetch='all')
    ok = 0
    erros = []
    for s in saloes:
        try:
            seed_novo_salao(s['id'], s['nome'])
            ok += 1
        except Exception as e:
            erros.append({'id': s['id'], 'erro': str(e)})
    return jsonify({'ok': True, 'resetados': ok, 'erros': erros})

@app.route('/api/superadmin/saloes/<int:sid>/seed', methods=['POST'])
def sa_seed_salao(sid):
    err = _require_sa()
    if err: return err
    salao = db_exec("SELECT * FROM saloes WHERE id=%s", (sid,), 'one')
    if not salao:
        return jsonify({'ok': False, 'erro': 'Salão não encontrado'})
    try:
        seed_novo_salao(sid, salao['nome'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)})

@app.route('/api/superadmin/saloes/<int:sid>/resetar-senha', methods=['POST'])
def sa_resetar_senha(sid):
    err = _require_sa()
    if err: return err
    d = request.json or {}
    nova = d.get('senha', 'admin')
    db_exec("UPDATE usuarios SET senha_hash=%s WHERE salon_id=%s AND login='admin'",
            (hash_senha(nova), sid))
    db_exec("UPDATE sistema_config SET valor=%s WHERE salon_id=%s AND chave='senha_admin'",
            (hash_senha(nova), sid))
    db_commit()
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════════════════════════
# ROTAS DO SALÃO (autenticação + todas as operações)
# ══════════════════════════════════════════════════════════════════════════════


@app.route('/entrar')
def entrar():
    if request.args.get('sair') == '1':
        session.clear()
    return send_from_directory('static', 'entrar.html')

@app.route('/')
def index():
    sid = request.args.get('salon_id', '').strip()
    if sid and sid != 'undefined' and sid != 'null':
        return send_from_directory('static', 'index.html')
    return send_from_directory('static', 'entrar.html')

# ─── LOGIN / SESSÃO ───────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.json or {}
    login_val = d.get('login','').strip()
    senha     = d.get('senha','')
    salon_id  = d.get('salon_id')  # frontend pode enviar
    if not login_val or not senha:
        return jsonify({'ok': False, 'erro': 'Preencha login e senha'})

    # Detectar salon_id pelo subdomínio ou parâmetro
    if not salon_id:
        host = request.host.split('.')[0]
        row = db_exec("SELECT id FROM saloes WHERE nome ILIKE %s AND ativo=1", (host,), 'one')
        if row:
            salon_id = row['id']

    if not salon_id:
        # Buscar salão pelo email OU telefone do usuário (cadastro self-service)
        so_dig = ''.join(ch for ch in login_val if ch.isdigit())
        u_any = db_exec(
            "SELECT salon_id FROM usuarios WHERE (login=LOWER(%s) OR (telefone!='' AND regexp_replace(telefone,'[^0-9]','','g')=%s)) AND ativo=1 LIMIT 1",
            (login_val, so_dig), 'one')
        if u_any:
            salon_id = u_any['salon_id']
        else:
            return jsonify({'ok': False, 'erro': 'Salão não identificado. Use a URL enviada pelo seu administrador.'})

    salon_id = int(salon_id)

    # Verificar se salão existe e está ativo
    salao = db_exec("SELECT * FROM saloes WHERE id=%s AND ativo=1", (salon_id,), 'one')
    if not salao:
        return jsonify({'ok': False, 'erro': 'Salão não encontrado ou inativo'})

    # 1) Login como usuário (admin/recepcionista) — por email OU telefone
    so_dig_u = ''.join(ch for ch in login_val if ch.isdigit())
    u = db_exec("""SELECT * FROM usuarios WHERE salon_id=%s AND ativo=1 AND
                   (login=%s OR login=LOWER(%s) OR (telefone!='' AND regexp_replace(telefone,'[^0-9]','','g')=%s))""",
                (salon_id, login_val, login_val, so_dig_u), 'one')
    if u and u['senha_hash'] == hash_senha(senha):
        session.permanent = True
        session['salon_id'] = salon_id
        session['salon_nome'] = salao['nome']
        session['uid']     = u['id']
        session['unome']   = u['nome']
        session['uperfil'] = u['perfil']
        session.pop('pro_id', None)
        return jsonify({'ok': True, 'nome': u['nome'], 'perfil': u['perfil'], 'salon_id': salon_id,
                        'permissoes': get_permissoes(dict(u)), 'salon_nome': salao['nome']})

    # 2) Login como profissional (por email)
    email = login_val.lower()
    p = db_exec("SELECT * FROM profissionais WHERE salon_id=%s AND LOWER(email)=%s AND ativo=1 AND pode_login=1",
                (salon_id, email), 'one')
    if p and p['senha_hash'] and p['senha_hash'] == hash_senha(senha):
        session.permanent = True
        session['salon_id']   = salon_id
        session['salon_nome'] = salao['nome']
        session['uid']        = -p['id']
        session['unome']      = p['nome']
        session['uperfil']    = 'profissional'
        session['pro_id']     = p['id']
        session['pro_ver_comissao'] = bool(p['pode_ver_comissao'])
        return jsonify({'ok': True, 'nome': p['nome'], 'perfil': 'profissional', 'salon_id': salon_id,
                        'permissoes': ['agenda_propria'] + (['comissao_propria'] if p['pode_ver_comissao'] else []),
                        'salon_nome': salao['nome']})

    return jsonify({'ok': False, 'erro': 'Usuário ou senha incorretos'})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/session', methods=['GET'])
def api_session():
    if 'uid' not in session or 'salon_id' not in session:
        return jsonify({'logado': False})
    sid = session['salon_id']
    if session.get('uperfil') == 'profissional' and session.get('pro_id'):
        p = db_exec("SELECT * FROM profissionais WHERE id=%s AND salon_id=%s AND ativo=1 AND pode_login=1",
                    (session['pro_id'], sid), 'one')
        if not p:
            session.clear()
            return jsonify({'logado': False})
        return jsonify({'logado': True, 'nome': p['nome'], 'perfil': 'profissional',
                        'pro_id': p['id'], 'salon_nome': session.get('salon_nome',''),
                        'salon_id': session.get('salon_id'),
                        'permissoes': ['agenda_propria'] + (['comissao_propria'] if p['pode_ver_comissao'] else [])})
    u = db_exec("SELECT * FROM usuarios WHERE id=%s AND salon_id=%s AND ativo=1",
                (session['uid'], sid), 'one')
    if not u:
        session.clear()
        return jsonify({'logado': False})
    return jsonify({'logado': True, 'nome': u['nome'], 'perfil': u['perfil'],
                    'salon_nome': session.get('salon_nome',''),
                    'salon_id': session.get('salon_id'),
                    'permissoes': get_permissoes(dict(u))})

# ─── SALÃO (dados do tenant logado) ──────────────────────────────────────────
@app.route('/api/salao', methods=['GET','PUT'])
def salao_config():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT * FROM saloes WHERE id=%s", (sid,), 'one')
        return jsonify(dict(row) if row else {})
    d = request.json
    db_exec("UPDATE saloes SET nome=%s,telefone=%s,endereco=%s,logo=%s,email=%s WHERE id=%s",
            (d.get('nome'), d.get('telefone',''), d.get('endereco',''),
             d.get('logo',''), d.get('email',''), sid))
    db_commit()
    return jsonify({'ok': True})

# ─── AUTH SENHA DO SALÃO ──────────────────────────────────────────────────────
@app.route('/api/auth/verificar', methods=['POST'])
def auth_verificar():
    sid, err = require_salon()
    if err: return err
    d = request.json
    row = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='senha_admin'", (sid,), 'one')
    usr = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='usuario_admin'", (sid,), 'one')
    if not row:
        return jsonify({'ok': False, 'erro': 'Configuração não encontrada'})
    usuario_ok = d.get('usuario','') == (usr['valor'] if usr else 'admin')
    senha_ok   = hash_senha(d.get('senha','')) == row['valor']
    return jsonify({'ok': usuario_ok and senha_ok})

@app.route('/api/auth/alterar', methods=['POST'])
def auth_alterar():
    sid, err = require_admin()
    if err: return err
    d = request.json
    atual = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='senha_admin'", (sid,), 'one')
    if not atual or hash_senha(d.get('senha_atual','')) != atual['valor']:
        return jsonify({'ok': False, 'erro': 'Senha atual incorreta'})
    db_exec("UPDATE sistema_config SET valor=%s WHERE salon_id=%s AND chave='usuario_admin'",
            (d.get('usuario','admin'), sid))
    db_exec("UPDATE sistema_config SET valor=%s WHERE salon_id=%s AND chave='senha_admin'",
            (hash_senha(d.get('nova_senha','admin')), sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/auth/config', methods=['GET'])
def auth_config():
    sid, err = require_salon()
    if err: return err
    usr = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='usuario_admin'", (sid,), 'one')
    eml = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='email_recuperacao'", (sid,), 'one')
    return jsonify({'usuario': usr['valor'] if usr else 'admin', 'email': eml['valor'] if eml else ''})

# ─── PROFISSIONAIS ────────────────────────────────────────────────────────────
@app.route('/api/profissionais', methods=['GET','POST'])
def profissionais():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT id,nome,cargo,cor,comissao_pct,h_inicio,h_fim,foto_base64,ativo,categorias,email,pode_login,pode_ver_comissao FROM profissionais WHERE salon_id=%s ORDER BY nome", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    email = (d.get('email','') or '').strip().lower()
    senha = d.get('senha','') or ''
    pode_login = 1 if d.get('pode_login') else 0
    pode_ver_comissao = 1 if d.get('pode_ver_comissao') else 0
    senha_hash_val = hash_senha(senha) if senha else ''
    if email:
        ex = db_exec("SELECT id FROM profissionais WHERE salon_id=%s AND LOWER(email)=%s AND email!=''", (sid,email), 'one')
        if ex:
            return jsonify({'ok': False, 'erro': 'Email já cadastrado para outro profissional'})
    cur = db_exec("""INSERT INTO profissionais (salon_id,nome,cargo,cor,comissao_pct,h_inicio,h_fim,foto_base64,ativo,email,senha_hash,pode_login,pode_ver_comissao)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s) RETURNING id""",
                  (sid,d['nome'],d.get('cargo',''),d.get('cor','#EC4899'),d.get('comissao_pct',40),
                   d.get('h_inicio','08:00'),d.get('h_fim','20:00'),d.get('foto_base64',''),
                   email,senha_hash_val,pode_login,pode_ver_comissao), 'one')
    db_commit()
    return jsonify({'ok': True, 'id': cur['id']})

@app.route('/api/profissionais/<int:pid>', methods=['GET','PUT','DELETE'])
def profissional(pid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT id,nome,cargo,cor,comissao_pct,h_inicio,h_fim,foto_base64,ativo,categorias,email,pode_login,pode_ver_comissao FROM profissionais WHERE id=%s AND salon_id=%s", (pid,sid), 'one')
        return jsonify(dict(row) if row else {})
    if request.method == 'DELETE':
        db_exec("UPDATE profissionais SET ativo=0, pode_login=0 WHERE id=%s AND salon_id=%s", (pid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    email = (d.get('email','') or '').strip().lower()
    senha = d.get('senha','') or ''
    pode_login = 1 if d.get('pode_login') else 0
    pode_ver_comissao = 1 if d.get('pode_ver_comissao') else 0
    if email:
        ex = db_exec("SELECT id FROM profissionais WHERE salon_id=%s AND LOWER(email)=%s AND id!=%s AND email!=''", (sid,email,pid), 'one')
        if ex:
            return jsonify({'ok': False, 'erro': 'Email já cadastrado para outro profissional'})
    if senha:
        db_exec("""UPDATE profissionais SET nome=%s,cargo=%s,cor=%s,comissao_pct=%s,h_inicio=%s,h_fim=%s,
                   foto_base64=%s,ativo=%s,email=%s,senha_hash=%s,pode_login=%s,pode_ver_comissao=%s WHERE id=%s AND salon_id=%s""",
                (d['nome'],d.get('cargo',''),d.get('cor','#EC4899'),d.get('comissao_pct',40),
                 d.get('h_inicio','08:00'),d.get('h_fim','20:00'),d.get('foto_base64',''),
                 d.get('ativo',1),email,hash_senha(senha),pode_login,pode_ver_comissao,pid,sid))
    else:
        db_exec("""UPDATE profissionais SET nome=%s,cargo=%s,cor=%s,comissao_pct=%s,h_inicio=%s,h_fim=%s,
                   foto_base64=%s,ativo=%s,email=%s,pode_login=%s,pode_ver_comissao=%s WHERE id=%s AND salon_id=%s""",
                (d['nome'],d.get('cargo',''),d.get('cor','#EC4899'),d.get('comissao_pct',40),
                 d.get('h_inicio','08:00'),d.get('h_fim','20:00'),d.get('foto_base64',''),
                 d.get('ativo',1),email,pode_login,pode_ver_comissao,pid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── MINHA AGENDA / COMISSÃO (profissional logado) ────────────────────────────
@app.route('/api/profissional/minha-agenda', methods=['GET'])
def minha_agenda():
    if session.get('uperfil') != 'profissional' or not session.get('pro_id'):
        return jsonify({'erro': 'Acesso negado'}), 403
    sid    = session['salon_id']
    pro_id = session['pro_id']
    periodo = request.args.get('periodo','dia')
    hoje = today_br()
    if periodo == 'semana':
        ini = hoje - datetime.timedelta(days=hoje.weekday())
        fim = ini + datetime.timedelta(days=6)
    else:
        ini = fim = hoje
    rows = db_exec("""SELECT a.id,a.data,a.h_ini,a.h_fim,a.status,c.nome as cliente,s.nome as servico
        FROM agendamentos a LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id
        WHERE a.salon_id=%s AND a.pro_id=%s AND a.data BETWEEN %s AND %s AND a.status!='cancelado'
        ORDER BY a.data,a.h_ini""", (sid,pro_id,ini.isoformat(),fim.isoformat()), 'all')
    return jsonify({'pro_id':pro_id,'periodo':periodo,'data_ini':ini.isoformat(),'data_fim':fim.isoformat(),'agenda':[dict(r) for r in rows]})

@app.route('/api/profissional/minha-comissao', methods=['GET'])
def minha_comissao():
    if session.get('uperfil') != 'profissional' or not session.get('pro_id'):
        return jsonify({'erro': 'Acesso negado'}), 403
    if not session.get('pro_ver_comissao'):
        return jsonify({'erro': 'Sem permissão'}), 403
    sid    = session['salon_id']
    pro_id = session['pro_id']
    hoje   = today_br()
    ini    = hoje.replace(day=1).isoformat()
    fim    = hoje.isoformat()
    total  = db_exec("SELECT COALESCE(SUM(c.valor),0) as t FROM comissoes c JOIN agendamentos a ON a.id=c.ag_id WHERE c.salon_id=%s AND c.pro_id=%s AND a.data BETWEEN %s AND %s", (sid,pro_id,ini,fim), 'one')
    qtd    = db_exec("SELECT COUNT(*) as n FROM agendamentos WHERE salon_id=%s AND pro_id=%s AND data BETWEEN %s AND %s AND status='concluido'", (sid,pro_id,ini,fim), 'one')
    return jsonify({'pro_id':pro_id,'periodo_ini':ini,'periodo_fim':fim,'total_comissao':float(total['t'] or 0),'qtd_atendimentos':int(qtd['n'] or 0)})

# ─── SERVIÇOS ─────────────────────────────────────────────────────────────────
@app.route('/api/servicos', methods=['GET','POST'])
def servicos():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM servicos WHERE salon_id=%s ORDER BY categoria,nome", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO servicos (salon_id,nome,categoria,duracao_min,preco,comissao_pct,ativo) VALUES (%s,%s,%s,%s,%s,%s,1)",
            (sid,d['nome'],d.get('categoria',''),d.get('duracao_min',60),d.get('preco',0),d.get('comissao_pct',40)))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/servicos/<int:svid>/retorno', methods=['PUT'])
def servico_retorno(svid):
    sid, err = require_salon()
    if err: return err
    dias = int((request.json or {}).get('dias', 0))
    db_exec("UPDATE servicos SET alerta_retorno_dias=%s WHERE id=%s AND salon_id=%s", (dias,svid,sid))
    db_commit()
    return jsonify({'ok': True, 'dias': dias})

@app.route('/api/servicos/<int:svid>', methods=['GET','PUT','DELETE'])
def servico(svid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT * FROM servicos WHERE id=%s AND salon_id=%s", (svid,sid), 'one')
        return jsonify(dict(row) if row else {})
    if request.method == 'DELETE':
        hoje = today_br().isoformat()
        ag = db_exec("SELECT COUNT(*) as n FROM agendamentos WHERE salon_id=%s AND svc_id=%s AND data>=%s AND status NOT IN ('cancelado','concluido')", (sid,svid,hoje), 'one')
        if ag['n'] > 0:
            return jsonify({'ok':False,'erro':'Serviço tem '+str(ag['n'])+' agendamento(s) futuro(s).'})
        db_exec("UPDATE servicos SET ativo=0 WHERE id=%s AND salon_id=%s", (svid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE servicos SET nome=%s,categoria=%s,duracao_min=%s,preco=%s,comissao_pct=%s,ativo=%s WHERE id=%s AND salon_id=%s",
            (d['nome'],d.get('categoria',''),d.get('duracao_min',60),d.get('preco',0),d.get('comissao_pct',40),d.get('ativo',1),svid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/servicos/categoria/<cat>', methods=['DELETE'])
def categoria_del(cat):
    sid, err = require_salon()
    if err: return err
    hoje = today_br().isoformat()
    ag = db_exec("SELECT COUNT(*) as n FROM agendamentos a JOIN servicos s ON s.id=a.svc_id WHERE a.salon_id=%s AND s.categoria=%s AND a.data>=%s AND a.status NOT IN ('cancelado','concluido')", (sid,cat,hoje), 'one')
    if ag['n'] > 0:
        return jsonify({'ok':False,'erro':'Categoria tem agendamentos futuros pendentes.'})
    db_exec("DELETE FROM servicos WHERE salon_id=%s AND categoria=%s", (sid,cat))
    db_commit()
    return jsonify({'ok': True})

# ─── CLIENTES ─────────────────────────────────────────────────────────────────
@app.route('/api/clientes', methods=['GET','POST'])
def clientes():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        q = request.args.get('q','').strip()
        if q:
            q_digits = ''.join(ch for ch in q if ch.isdigit())
            if q_digits and len(q_digits) >= 3:
                like_d = '%' + q_digits + '%'
                rows = db_exec("""SELECT * FROM clientes WHERE salon_id=%s AND ativo=1
                    AND (nome ILIKE %s OR REGEXP_REPLACE(tel,'[^0-9]','','g') LIKE %s) ORDER BY nome LIMIT 30""",
                    (sid, '%'+q+'%', like_d), 'all')
            else:
                rows = db_exec("SELECT * FROM clientes WHERE salon_id=%s AND ativo=1 AND (nome ILIKE %s OR tel ILIKE %s OR email ILIKE %s) ORDER BY nome LIMIT 30",
                    (sid,'%'+q+'%','%'+q+'%','%'+q+'%'), 'all')
        else:
            rows = db_exec("SELECT * FROM clientes WHERE salon_id=%s AND ativo=1 ORDER BY nome", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    tel = d.get('tel','')
    if tel:
        ex = db_exec("SELECT id FROM clientes WHERE salon_id=%s AND tel=%s AND tel!=''", (sid,tel), 'one')
        if ex:
            return jsonify({'ok':False,'erro':'Telefone já cadastrado','id':ex['id']}), 409
    hoje = today_br().isoformat()
    cur = db_exec("INSERT INTO clientes (salon_id,nome,tel,email,cpf,nasc,obs,ativo,criado_em) VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
                  (sid,d['nome'],tel,d.get('email',''),d.get('cpf',''),d.get('nasc',''),d.get('obs',''),hoje), 'one')
    db_commit()
    return jsonify({'ok': True, 'id': cur['id']})

@app.route('/api/clientes/<int:cid>', methods=['GET','PUT','DELETE'])
def cliente(cid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT * FROM clientes WHERE id=%s AND salon_id=%s", (cid,sid), 'one')
        ags = db_exec("""SELECT a.*,s.nome as svc_nome,p.nome as pro_nome FROM agendamentos a
            LEFT JOIN servicos s ON s.id=a.svc_id LEFT JOIN profissionais p ON p.id=a.pro_id
            WHERE a.cli_id=%s AND a.salon_id=%s ORDER BY a.data DESC,a.h_ini DESC LIMIT 50""", (cid,sid), 'all')
        total = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE cli_id=%s AND salon_id=%s AND status='concluido'", (cid,sid), 'one')
        if not row: return jsonify({}), 404
        return jsonify({'cliente':dict(row),'historico':[dict(a) for a in ags],'total_gasto':total['t']})
    if request.method == 'DELETE':
        db_exec("UPDATE clientes SET ativo=0 WHERE id=%s AND salon_id=%s", (cid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE clientes SET nome=%s,tel=%s,email=%s,cpf=%s,nasc=%s,obs=%s,ativo=%s WHERE id=%s AND salon_id=%s",
            (d['nome'],d.get('tel',''),d.get('email',''),d.get('cpf',''),d.get('nasc',''),d.get('obs',''),d.get('ativo',1),cid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── AGENDAMENTOS ─────────────────────────────────────────────────────────────
@app.route('/api/agendamentos', methods=['GET','POST'])
def agendamentos():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        data   = request.args.get('data','')
        pro_id = request.args.get('pro_id','')
        cli_id = request.args.get('cli_id','')
        ini    = request.args.get('ini','')
        fim    = request.args.get('fim','')
        q = """SELECT a.*,c.nome as cli_nome,c.tel as cli_tel,s.nome as svc_nome,s.duracao_min,
            p.nome as pro_nome,p.cor as pro_cor,
            (SELECT COUNT(*) FROM lembrete_log ll WHERE ll.ag_id=a.id) as lembrete_enviado
            FROM agendamentos a
            LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id
            LEFT JOIN profissionais p ON p.id=a.pro_id WHERE a.salon_id=%s"""
        params = [sid]
        if data:   q += " AND a.data=%s"; params.append(data)
        if pro_id: q += " AND a.pro_id=%s"; params.append(pro_id)
        if cli_id: q += " AND a.cli_id=%s"; params.append(cli_id)
        if ini:    q += " AND a.data>=%s"; params.append(ini)
        if fim:    q += " AND a.data<=%s"; params.append(fim)
        q += " ORDER BY a.data DESC,a.h_ini DESC"
        rows = db_exec(q, params, 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    cur = db_exec("INSERT INTO agendamentos (salon_id,cli_id,pro_id,svc_id,data,h_ini,h_fim,preco,status,obs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (sid,d['cli_id'],d['pro_id'],d['svc_id'],d['data'],d['h_ini'],d['h_fim'],d.get('preco',0),d.get('status','agendado'),d.get('obs','')), 'one')
    db_commit()
    return jsonify({'ok': True, 'id': cur['id']})

@app.route('/api/agendamentos/<int:aid>', methods=['GET','PUT','DELETE'])
def agendamento(aid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("""SELECT a.*,c.nome as cli_nome,c.tel as cli_tel,s.nome as svc_nome,s.duracao_min,
            p.nome as pro_nome,p.cor as pro_cor FROM agendamentos a
            LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id
            LEFT JOIN profissionais p ON p.id=a.pro_id WHERE a.id=%s AND a.salon_id=%s""", (aid,sid), 'one')
        return jsonify(dict(row) if row else {}), (200 if row else 404)
    if request.method == 'DELETE':
        ag = db_exec("""SELECT a.*,c.nome as cli_nome,p.nome as pro_nome,s.nome as svc_nome FROM agendamentos a
            LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN profissionais p ON p.id=a.pro_id
            LEFT JOIN servicos s ON s.id=a.svc_id WHERE a.id=%s AND a.salon_id=%s""", (aid,sid), 'one')
        if not ag: return jsonify({'ok':False,'erro':'Não encontrado'}), 404
        if ag['status'] == 'concluido':
            return jsonify({'ok':False,'erro':'bloqueado','msg':'Atendimentos concluídos não podem ser excluídos.'}), 403
        db_exec("""INSERT INTO lixeira_agendamentos (salon_id,ag_id,cli_nome,pro_nome,svc_nome,data,h_ini,h_fim,preco,status,pag,obs,excluido_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (sid,aid,ag['cli_nome'] or '',ag['pro_nome'] or '',ag['svc_nome'] or '',
             ag['data'] or '',ag['h_ini'] or '',ag['h_fim'] or '',ag['preco'] or 0,
             ag['status'] or '',ag['pag'] or '',ag['obs'] or '','admin'))
        db_exec("DELETE FROM agendamentos WHERE id=%s AND salon_id=%s", (aid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    ag_atual = db_exec("SELECT status FROM agendamentos WHERE id=%s AND salon_id=%s", (aid,sid), 'one')
    if ag_atual and ag_atual['status'] == 'concluido' and not d.get('reaberto'):
        return jsonify({'ok':False,'erro':'bloqueado','msg':'Atendimento concluído. Use reabrir para editar.'}), 403
    db_exec("UPDATE agendamentos SET cli_id=%s,pro_id=%s,svc_id=%s,data=%s,h_ini=%s,h_fim=%s,preco=%s,status=%s,obs=%s WHERE id=%s AND salon_id=%s",
            (d['cli_id'],d['pro_id'],d['svc_id'],d['data'],d['h_ini'],d['h_fim'],d.get('preco',0),d.get('status','agendado'),d.get('obs',''),aid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/agendamentos/<int:aid>/reabrir', methods=['POST'])
def reabrir(aid):
    sid, err = require_salon()
    if err: return err
    d = request.json
    cfg = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='senha_admin'", (sid,), 'one')
    usr = db_exec("SELECT valor FROM sistema_config WHERE salon_id=%s AND chave='usuario_admin'", (sid,), 'one')
    if not cfg: return jsonify({'ok':False,'erro':'Configuração não encontrada'})
    if d.get('usuario','') != (usr['valor'] if usr else 'admin') or hash_senha(d.get('senha','')) != cfg['valor']:
        return jsonify({'ok':False,'erro':'Usuário ou senha incorretos'})
    db_exec("UPDATE agendamentos SET status='confirmado' WHERE id=%s AND salon_id=%s", (aid,sid))
    db_exec("DELETE FROM comissoes WHERE ag_id=%s AND salon_id=%s", (aid,sid))
    db_exec("DELETE FROM caixa WHERE ag_id=%s AND salon_id=%s", (aid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/agendamentos/<int:aid>/finalizar', methods=['POST'])
def finalizar(aid):
    sid, err = require_salon()
    if err: return err
    d      = request.json
    pag    = d.get('pag','Dinheiro')
    gorjeta = float(d.get('gorjeta',0))
    preco  = float(d.get('preco',0))
    ag = db_exec("SELECT * FROM agendamentos WHERE id=%s AND salon_id=%s", (aid,sid), 'one')
    if not ag: return jsonify({'erro':'Não encontrado'}), 404
    taxa_row = db_exec("SELECT taxa_pct FROM taxas WHERE salon_id=%s AND pag=%s", (sid,pag), 'one')
    taxa_pct = taxa_row['taxa_pct'] if taxa_row else 0
    desconto_taxa = round(preco * taxa_pct / 100, 2)
    preco_liq     = round(preco - desconto_taxa + gorjeta, 2)
    com_pct = 0
    override = db_exec("SELECT comissao_override FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s AND svc_id=%s", (sid,ag['pro_id'],ag['svc_id']), 'one')
    if override and override['comissao_override'] >= 0:
        com_pct = override['comissao_override']
    else:
        svc = db_exec("SELECT categoria FROM servicos WHERE id=%s AND salon_id=%s", (ag['svc_id'],sid), 'one')
        if svc and svc['categoria']:
            cat_row = db_exec("SELECT comissao_override FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s AND categoria=%s AND (svc_id=0 OR svc_id IS NULL)", (sid,ag['pro_id'],svc['categoria']), 'one')
            if cat_row and cat_row['comissao_override'] >= 0:
                com_pct = cat_row['comissao_override']
    comissao_val = round(preco_liq * com_pct / 100, 2)
    db_exec("UPDATE agendamentos SET status='concluido',pag=%s,gorjeta=%s,preco=%s WHERE id=%s AND salon_id=%s", (pag,gorjeta,preco,aid,sid))
    db_exec("INSERT INTO caixa (salon_id,data,tipo,descricao,valor,pag,ag_id) VALUES (%s,%s,'entrada',%s,%s,%s,%s)",
            (sid,ag['data'],'Atendimento #'+str(aid),preco_liq,pag,aid))
    # Consolidar todas as taxas de cartão num único lançamento mensal
    if desconto_taxa > 0:
        import datetime as _dt_taxa
        data_ag = _dt_taxa.date.fromisoformat(ag['data'])
        mes_ref = data_ag.strftime('%Y-%m')
        primeiro_dia = data_ag.replace(day=1).isoformat()
        desc_mes = 'Taxas de Cartão — ' + mes_ref
        ex = db_exec(
            "SELECT id, valor FROM despesas WHERE salon_id=%s AND descricao=%s AND categoria='Taxas de Cartão'",
            (sid, desc_mes), 'one')
        if ex:
            novo_valor = round(float(ex['valor']) + desconto_taxa, 2)
            db_exec("UPDATE despesas SET valor=%s WHERE id=%s", (novo_valor, ex['id']))
        else:
            db_exec("INSERT INTO despesas (salon_id,data,descricao,valor,categoria,pago,vencimento) VALUES (%s,%s,%s,%s,'Taxas de Cartão',1,%s)",
                    (sid, primeiro_dia, desc_mes, desconto_taxa, primeiro_dia))
    ex_com = db_exec("SELECT id FROM comissoes WHERE ag_id=%s AND salon_id=%s", (aid,sid), 'one')
    if not ex_com:
        db_exec("INSERT INTO comissoes (salon_id,pro_id,ag_id,valor,pago,com_pct) VALUES (%s,%s,%s,%s,0,%s)",
                (sid,ag['pro_id'],aid,comissao_val,com_pct))
    db_exec("UPDATE clientes SET ultima_visita=%s WHERE id=%s AND salon_id=%s", (ag['data'],ag['cli_id'],sid))
    try:
        svc_r = db_exec("SELECT alerta_retorno_dias,nome FROM servicos WHERE id=%s AND salon_id=%s", (ag['svc_id'],sid), 'one')
        if svc_r and int(svc_r['alerta_retorno_dias'] or 0) > 0:
            dias_r   = int(svc_r['alerta_retorno_dias'])
            data_ret = (datetime.date.fromisoformat(ag['data']) + datetime.timedelta(days=dias_r)).isoformat()
            db_exec("DELETE FROM retorno_alertas WHERE salon_id=%s AND cli_id=%s AND svc_id=%s AND realizado=0", (sid,ag['cli_id'],ag['svc_id']))
            db_exec("INSERT INTO retorno_alertas (salon_id,cli_id,ag_id,svc_id,svc_nome,data_atendimento,data_retorno,dias,realizado) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)",
                    (sid,ag['cli_id'],aid,ag['svc_id'],svc_r['nome'],ag['data'],data_ret,dias_r))
    except: pass
    db_commit()
    return jsonify({'ok': True, 'taxa_pct': taxa_pct, 'comissao': comissao_val})

# ─── COMANDAS ─────────────────────────────────────────────────────────────────
@app.route('/api/comandas', methods=['GET'])
def comandas():
    sid, err = require_salon()
    if err: return err
    ini    = request.args.get('ini',(today_br()-datetime.timedelta(days=30)).isoformat())
    fim    = request.args.get('fim',today_br().isoformat())
    pro_id = request.args.get('pro_id','')
    q = """SELECT a.id,a.data,a.h_ini,a.h_fim,a.preco,a.pag,a.gorjeta,a.status,
        c.nome as cli_nome,c.tel as cli_tel,s.nome as svc_nome,
        p.nome as pro_nome,p.cor as pro_cor,co.valor as comissao,co.pago as com_pago,co.id as com_id
        FROM agendamentos a LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id
        LEFT JOIN profissionais p ON p.id=a.pro_id LEFT JOIN comissoes co ON co.ag_id=a.id
        WHERE a.salon_id=%s AND a.status='concluido' AND a.data>=%s AND a.data<=%s"""
    params = [sid,ini,fim]
    if pro_id: q += " AND a.pro_id=%s"; params.append(pro_id)
    q += " ORDER BY a.data DESC,a.h_ini DESC"
    rows  = db_exec(q, params, 'all')
    total = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND data<=%s AND status='concluido'", (sid,ini,fim), 'one')
    return jsonify({'comandas':[dict(r) for r in rows],'total':round(total['t'],2)})

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
@app.route('/api/dashboard')
def dashboard():
    sid, err = require_salon()
    if err: return err
    hoje    = today_br().isoformat()
    ini_sem = (today_br()-datetime.timedelta(days=today_br().weekday())).isoformat()
    ini_mes = today_br().replace(day=1).isoformat()
    fat_row = db_exec(
        "SELECT COALESCE(SUM(CASE WHEN data=%s THEN preco ELSE 0 END),0) as hoje,"
        " COALESCE(SUM(CASE WHEN data>=%s THEN preco ELSE 0 END),0) as sem,"
        " COALESCE(SUM(preco),0) as mes"
        " FROM agendamentos WHERE salon_id=%s AND status='concluido' AND data>=%s",
        (hoje, ini_sem, sid, ini_mes), 'one')
    fat_hoje = float(fat_row['hoje'] or 0) if fat_row else 0
    fat_sem  = float(fat_row['sem']  or 0) if fat_row else 0
    fat_mes  = float(fat_row['mes']  or 0) if fat_row else 0
    ags_hoje = db_exec("""SELECT a.*,c.nome as cli_nome,s.nome as svc_nome,p.nome as pro_nome,p.cor as pro_cor
        FROM agendamentos a LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id LEFT JOIN profissionais p ON p.id=a.pro_id
        WHERE a.salon_id=%s AND a.data=%s ORDER BY a.h_ini""", (sid,hoje), 'all')
    estoque_baixo = db_exec("SELECT * FROM estoque WHERE salon_id=%s AND quantidade<=minimo", (sid,), 'all')
    ini_graf = (today_br()-datetime.timedelta(days=29)).isoformat()
    graf_rows = db_exec(
        "SELECT data, COALESCE(SUM(valor),0) as t FROM caixa WHERE salon_id=%s AND data>=%s AND data<=%s AND tipo='entrada' GROUP BY data",
        (sid, ini_graf, hoje), 'all')
    graf_map = {r['data']: round(float(r['t']),2) for r in graf_rows}
    grafico = []
    for i in range(29,-1,-1):
        d = (today_br()-datetime.timedelta(days=i)).isoformat()
        grafico.append({'data':d,'valor':graf_map.get(d,0.0)})
    total_clientes = db_exec("SELECT COUNT(*) as t FROM clientes WHERE salon_id=%s AND ativo=1", (sid,), 'one')['t']
    total_ags_mes  = db_exec("SELECT COUNT(*) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND status!='cancelado'", (sid,ini_mes), 'one')['t']
    novos_mes      = db_exec("SELECT COUNT(*) as t FROM clientes WHERE salon_id=%s AND criado_em>=%s AND ativo=1", (sid,ini_mes), 'one')['t']
    top_clientes   = db_exec("""SELECT c.nome,COALESCE(SUM(a.preco),0) as total,COUNT(*) as visitas FROM agendamentos a JOIN clientes c ON c.id=a.cli_id
        WHERE a.salon_id=%s AND a.data>=%s AND a.status='concluido' GROUP BY a.cli_id,c.nome ORDER BY total DESC LIMIT 5""", (sid,ini_mes), 'all')
    top_servicos   = db_exec("""SELECT s.nome,s.categoria,COUNT(*) as qtd,COALESCE(SUM(a.preco),0) as total FROM agendamentos a JOIN servicos s ON s.id=a.svc_id
        WHERE a.salon_id=%s AND a.data>=%s AND a.status='concluido' GROUP BY a.svc_id,s.nome,s.categoria ORDER BY qtd DESC LIMIT 8""", (sid,ini_mes), 'all')
    fat_por_pro    = db_exec("""SELECT p.nome,p.cor,COALESCE(SUM(a.preco),0) as total,COUNT(*) as qtd FROM agendamentos a JOIN profissionais p ON p.id=a.pro_id
        WHERE a.salon_id=%s AND a.data>=%s AND a.status='concluido' GROUP BY a.pro_id,p.nome,p.cor ORDER BY total DESC""", (sid,ini_mes), 'all')
    return jsonify({
        'fat_hoje':round(fat_hoje,2),'fat_sem':round(fat_sem,2),'fat_mes':round(fat_mes,2),
        'ags_hoje':[dict(r) for r in ags_hoje],'estoque_baixo':[dict(r) for r in estoque_baixo],
        'grafico':grafico,'total_clientes':total_clientes,'total_ags_mes':total_ags_mes,
        'novos_mes':novos_mes,'top_clientes':[dict(r) for r in top_clientes],
        'top_servicos':[dict(r) for r in top_servicos],'fat_por_pro':[dict(r) for r in fat_por_pro],
    })

# ─── CAIXA ────────────────────────────────────────────────────────────────────
@app.route('/api/caixa', methods=['GET','POST'])
def caixa():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        ini = request.args.get('ini', today_br().isoformat())
        fim = request.args.get('fim', today_br().isoformat())
        rows = db_exec("SELECT * FROM caixa WHERE salon_id=%s AND data>=%s AND data<=%s ORDER BY data DESC,id DESC", (sid,ini,fim), 'all')
        tot_ent = db_exec("SELECT COALESCE(SUM(valor),0) as t FROM caixa WHERE salon_id=%s AND data>=%s AND data<=%s AND tipo='entrada'", (sid,ini,fim), 'one')['t']
        tot_sai = db_exec("SELECT COALESCE(SUM(valor),0) as t FROM caixa WHERE salon_id=%s AND data>=%s AND data<=%s AND tipo='saida'", (sid,ini,fim), 'one')['t']
        return jsonify({'movimentos':[dict(r) for r in rows],'total_entradas':round(tot_ent,2),'total_saidas':round(tot_sai,2),'saldo':round(tot_ent-tot_sai,2)})
    d = request.json
    db_exec("INSERT INTO caixa (salon_id,data,tipo,descricao,valor,pag) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid,d.get('data',today_br().isoformat()),d['tipo'],d.get('descricao',''),d.get('valor',0),d.get('pag','')))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/caixa/<int:cid>', methods=['DELETE'])
def caixa_del(cid):
    sid, err = require_salon()
    if err: return err
    db_exec("DELETE FROM caixa WHERE id=%s AND salon_id=%s", (cid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── DESPESAS ─────────────────────────────────────────────────────────────────
@app.route('/api/despesas', methods=['GET','POST'])
def despesas():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        ini = request.args.get('ini', today_br().replace(day=1).isoformat())
        fim = request.args.get('fim', today_br().isoformat())
        rows = db_exec("SELECT * FROM despesas WHERE salon_id=%s AND data>=%s AND data<=%s ORDER BY data DESC", (sid,ini,fim), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO despesas (salon_id,data,descricao,valor,categoria,pago,vencimento) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (sid,d.get('data',today_br().isoformat()),d['descricao'],d.get('valor',0),d.get('categoria',''),d.get('pago',0),d.get('vencimento','')))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/despesas/<int:did>', methods=['PUT','DELETE'])
def despesa(did):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("DELETE FROM despesas WHERE id=%s AND salon_id=%s", (did,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE despesas SET data=%s,descricao=%s,valor=%s,categoria=%s,pago=%s,vencimento=%s,data_pagamento=%s WHERE id=%s AND salon_id=%s",
            (d.get('data'),d.get('descricao'),d.get('valor',0),d.get('categoria',''),d.get('pago',0),d.get('vencimento',''),d.get('data_pagamento',''),did,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── COMISSÕES ────────────────────────────────────────────────────────────────
@app.route('/api/comissoes', methods=['GET'])
def comissoes_list():
    sid, err = require_salon()
    if err: return err
    ini    = request.args.get('ini', today_br().replace(day=1).isoformat())
    fim    = request.args.get('fim', today_br().isoformat())
    pro_id = request.args.get('pro_id','')
    q = """SELECT c.*,a.data,a.h_ini,a.pag,
        COALESCE(NULLIF(a.preco,0), s.preco, 0) as svc_preco,
        s.nome as svc_nome,cl.nome as cli_nome,p.nome as pro_nome
        FROM comissoes c LEFT JOIN agendamentos a ON a.id=c.ag_id LEFT JOIN servicos s ON s.id=a.svc_id
        LEFT JOIN clientes cl ON cl.id=a.cli_id LEFT JOIN profissionais p ON p.id=c.pro_id
        WHERE c.salon_id=%s AND a.data>=%s AND a.data<=%s"""
    params = [sid,ini,fim]
    if pro_id: q += " AND c.pro_id=%s"; params.append(pro_id)
    q += " ORDER BY a.data DESC,a.h_ini DESC"
    rows = db_exec(q, params, 'all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/comissoes/<int:cid>/pagar', methods=['POST'])
def comissao_pagar(cid):
    sid, err = require_salon()
    if err: return err
    db_exec("UPDATE comissoes SET pago=1,data_pagamento=%s WHERE id=%s AND salon_id=%s",
            (today_br().isoformat(),cid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/comissoes/resumo-pdf', methods=['POST'])
def comissoes_resumo_pdf():
    sid, err = require_salon()
    if err: return err
    d      = request.json or {}
    pro_id = d.get('pro_id')
    ini    = d.get('ini')
    fim    = d.get('fim')
    pro    = db_exec("SELECT * FROM profissionais WHERE id=%s AND salon_id=%s", (pro_id,sid), 'one')
    salao  = db_exec("SELECT nome FROM saloes WHERE id=%s", (sid,), 'one')
    rows   = db_exec("""SELECT c.id,c.valor,c.pago,c.com_pct,a.data,a.h_ini,a.preco as ag_preco,
        a.gorjeta as ag_gorjeta,a.pag,s.nome as svc_nome,cl.nome as cli_nome,COALESCE(t.taxa_pct,0) as taxa_pct
        FROM comissoes c LEFT JOIN agendamentos a ON a.id=c.ag_id LEFT JOIN servicos s ON s.id=a.svc_id
        LEFT JOIN clientes cl ON cl.id=a.cli_id LEFT JOIN taxas t ON t.pag=a.pag AND t.salon_id=c.salon_id
        WHERE c.salon_id=%s AND c.pro_id=%s AND a.data>=%s AND a.data<=%s ORDER BY a.data,a.h_ini""",
        (sid,pro_id,ini,fim), 'all')
    rows_out = []
    for r in rows:
        ag_preco = float(r['ag_preco'] or 0)
        taxa_pct = float(r['taxa_pct'] or 0)
        com_pct  = float(r['com_pct'] or 0)
        comissao = float(r['valor'] or 0)
        gorjeta  = float(r['ag_gorjeta'] or 0)
        desconto_taxa = round(ag_preco * taxa_pct / 100, 2)
        valor_liquido = round(ag_preco - desconto_taxa, 2)
        rows_out.append({'data':r['data'] or '','cli_nome':r['cli_nome'] or '---','svc_nome':r['svc_nome'] or '---',
            'pag':r['pag'] or '---','taxa_pct':taxa_pct,'com_pct':com_pct,'ag_preco':ag_preco,'gorjeta':gorjeta,
            'desconto_taxa':desconto_taxa,'valor_liquido':valor_liquido,'comissao':comissao,'pago':bool(r['pago'])})
    total_bruto     = sum(r['ag_preco'] for r in rows_out)
    total_desc_taxa = sum(r['desconto_taxa'] for r in rows_out)
    total_gorjeta   = sum(r['gorjeta'] for r in rows_out)
    total_liquido   = sum(r['valor_liquido'] for r in rows_out)
    total_comissao  = sum(r['comissao'] for r in rows_out)
    adiantamentos   = sum(r['comissao'] for r in rows_out if r['pago'])
    return jsonify({'profissional':dict(pro) if pro else {},'salon_nome':salao['nome'] if salao else '',
        'ini':ini,'fim':fim,'comissoes':rows_out,'qtd':len(rows_out),
        'total_bruto':round(total_bruto,2),'total_desc_taxa':round(total_desc_taxa,2),
        'total_gorjeta':round(total_gorjeta,2),'total_liquido':round(total_liquido,2),
        'total_comissao':round(total_comissao,2),'adiantamentos':round(adiantamentos,2),
        'a_receber':round(total_comissao-adiantamentos,2)})

# ─── FATURAMENTO ──────────────────────────────────────────────────────────────
@app.route('/api/faturamento')
def faturamento():
    sid, err = require_salon()
    if err: return err
    ini = request.args.get('ini', today_br().replace(day=1).isoformat())
    fim = request.args.get('fim', today_br().isoformat())
    receitas  = db_exec("SELECT COALESCE(SUM(valor),0) as t FROM caixa WHERE salon_id=%s AND data>=%s AND data<=%s AND tipo='entrada'", (sid,ini,fim), 'one')['t']
    despesas_ = db_exec("SELECT COALESCE(SUM(valor),0) as t FROM despesas WHERE salon_id=%s AND data>=%s AND data<=%s", (sid,ini,fim), 'one')['t']
    por_pag   = db_exec("SELECT pag,COUNT(*) as qtd,COALESCE(SUM(preco),0) as total FROM agendamentos WHERE salon_id=%s AND data>=%s AND data<=%s AND status='concluido' GROUP BY pag ORDER BY total DESC", (sid,ini,fim), 'all')
    por_pro   = db_exec("""SELECT p.nome,p.cor,COUNT(*) as qtd,COALESCE(SUM(a.preco),0) as total FROM agendamentos a JOIN profissionais p ON p.id=a.pro_id
        WHERE a.salon_id=%s AND a.data>=%s AND a.data<=%s AND a.status='concluido' GROUP BY a.pro_id,p.nome,p.cor ORDER BY total DESC""", (sid,ini,fim), 'all')
    por_svc   = db_exec("""SELECT s.nome,s.categoria,COUNT(*) as qtd,COALESCE(SUM(a.preco),0) as total FROM agendamentos a JOIN servicos s ON s.id=a.svc_id
        WHERE a.salon_id=%s AND a.data>=%s AND a.data<=%s AND a.status='concluido' GROUP BY a.svc_id,s.nome,s.categoria ORDER BY total DESC""", (sid,ini,fim), 'all')
    return jsonify({'receitas':round(receitas,2),'despesas':round(despesas_,2),'lucro':round(receitas-despesas_,2),
        'por_pagamento':[dict(r) for r in por_pag],'por_profissional':[dict(r) for r in por_pro],'por_servico':[dict(r) for r in por_svc]})

@app.route('/api/faturamento/mensal')
def faturamento_mensal():
    sid, err = require_salon()
    if err: return err
    meses = []
    for i in range(11,-1,-1):
        d = today_br().replace(day=1) - datetime.timedelta(days=i*28)
        mes_ini = d.replace(day=1).isoformat()
        prox = d.replace(day=28) + datetime.timedelta(days=4)
        mes_fim = (prox - datetime.timedelta(days=prox.day)).isoformat()
        fat = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND data<=%s AND status='concluido'", (sid,mes_ini,mes_fim), 'one')['t']
        meses.append({'mes':d.strftime('%Y-%m'),'label':d.strftime('%b/%y'),'total':round(fat,2)})
    return jsonify(meses)

# ─── RETORNO ALERTAS ──────────────────────────────────────────────────────────
@app.route('/api/retorno-alertas', methods=['GET'])
def retorno_alertas():
    sid, err = require_salon()
    if err: return err
    hoje = today_br().isoformat()
    dias_ahead = int(request.args.get('dias', 7))
    futuro = (today_br() + datetime.timedelta(days=dias_ahead)).isoformat()
    rows = db_exec("""SELECT ra.*, c.nome as cli_nome, c.tel as cli_tel,
        (SELECT MAX(enviado_em) FROM contato_auto_log cal
         WHERE cal.salon_id=ra.salon_id AND cal.tipo='retorno' AND cal.cli_id=ra.cli_id) as enviado_em
        FROM retorno_alertas ra
        JOIN clientes c ON c.id=ra.cli_id WHERE ra.salon_id=%s AND ra.realizado=0 AND ra.data_retorno<=%s
        ORDER BY ra.data_retorno""", (sid,futuro), 'all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/retorno-alertas/<int:rid>/realizado', methods=['POST'])
def retorno_realizado(rid):
    sid, err = require_salon()
    if err: return err
    db_exec("UPDATE retorno_alertas SET realizado=1 WHERE id=%s AND salon_id=%s", (rid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/retorno-alertas/<int:rid>', methods=['DELETE', 'PUT'])
def retorno_alerta_edit(rid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("DELETE FROM retorno_alertas WHERE id=%s AND salon_id=%s", (rid, sid))
        db_commit()
        return jsonify({'ok': True})
    # PUT: marcar como realizado (ou outros campos)
    d = request.json or {}
    if 'realizado' in d:
        db_exec("UPDATE retorno_alertas SET realizado=%s WHERE id=%s AND salon_id=%s",
                (int(d.get('realizado', 1)), rid, sid))
        db_commit()
    return jsonify({'ok': True})

# ─── CLIENTES INATIVOS ────────────────────────────────────────────────────────
@app.route('/api/clientes/inativos', methods=['GET'])
def clientes_inativos():
    sid, err = require_salon()
    if err: return err
    dias = int(request.args.get('dias', 40))
    corte = (today_br()-datetime.timedelta(days=dias)).isoformat()
    rows = db_exec("""SELECT c.*,
        (SELECT COUNT(*) FROM agendamentos WHERE salon_id=%s AND cli_id=c.id AND status='concluido') as total_ags,
        (SELECT COALESCE(SUM(preco),0) FROM agendamentos WHERE salon_id=%s AND cli_id=c.id AND status='concluido') as total_gasto,
        (SELECT COUNT(*) FROM contato_auto_log cal WHERE cal.salon_id=%s AND cal.tipo='inativo' AND cal.cli_id=c.id) as msgs_enviadas,
        (SELECT MAX(enviado_em) FROM contato_auto_log cal WHERE cal.salon_id=%s AND cal.tipo='inativo' AND cal.cli_id=c.id) as ultimo_envio
        FROM clientes c WHERE c.salon_id=%s AND c.ativo=1 AND (c.ultima_visita IS NULL OR c.ultima_visita='' OR c.ultima_visita<=%s)
        ORDER BY c.ultima_visita ASC NULLS FIRST""", (sid,sid,sid,sid,sid,corte), 'all')
    return jsonify([dict(r) for r in rows])

# ─── DUPLICATAS ───────────────────────────────────────────────────────────────
@app.route('/api/clientes/duplicatas', methods=['GET'])
def clientes_duplicatas():
    sid, err = require_salon()
    if err: return err
    rows = db_exec("""SELECT tel,COUNT(*) as qtd,STRING_AGG(nome,' | ') as nomes,STRING_AGG(id::text,',') as ids
        FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1 GROUP BY tel HAVING COUNT(*)>1 ORDER BY qtd DESC""", (sid,), 'all')
    return jsonify([dict(r) for r in rows])

# ─── ESTOQUE ──────────────────────────────────────────────────────────────────
@app.route('/api/estoque', methods=['GET','POST'])
def estoque():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM estoque WHERE salon_id=%s ORDER BY categoria,nome", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO estoque (salon_id,nome,categoria,quantidade,minimo,unidade) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid,d['nome'],d.get('categoria',''),d.get('quantidade',0),d.get('minimo',0),d.get('unidade','un')))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/estoque/<int:eid>', methods=['PUT','DELETE'])
def estoque_item(eid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("DELETE FROM estoque WHERE id=%s AND salon_id=%s", (eid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE estoque SET nome=%s,categoria=%s,quantidade=%s,minimo=%s,unidade=%s WHERE id=%s AND salon_id=%s",
            (d['nome'],d.get('categoria',''),d.get('quantidade',0),d.get('minimo',0),d.get('unidade','un'),eid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── TAXAS ────────────────────────────────────────────────────────────────────
@app.route('/api/taxas', methods=['GET','POST'])
def taxas():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM taxas WHERE salon_id=%s ORDER BY pag", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO taxas (salon_id,pag,taxa_pct) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (sid,d['pag'],d.get('taxa_pct',0)))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/taxas/<int:tid>', methods=['PUT','DELETE'])
def taxa(tid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("DELETE FROM taxas WHERE id=%s AND salon_id=%s", (tid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE taxas SET pag=%s,taxa_pct=%s WHERE id=%s AND salon_id=%s", (d['pag'],d.get('taxa_pct',0),tid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── WHATSAPP TEMPLATES ───────────────────────────────────────────────────────
@app.route('/api/wpp/templates', methods=['GET','POST'])
def wpp_templates():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM wpp_templates WHERE salon_id=%s AND ativo=1 ORDER BY nome", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO wpp_templates (salon_id,nome,tipo,mensagem) VALUES (%s,%s,%s,%s)",
            (sid,d['nome'],d.get('tipo','aviso'),d['mensagem']))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp/templates/<int:tid>', methods=['PUT','DELETE'])
def wpp_template(tid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("UPDATE wpp_templates SET ativo=0 WHERE id=%s AND salon_id=%s", (tid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json
    db_exec("UPDATE wpp_templates SET nome=%s,tipo=%s,mensagem=%s WHERE id=%s AND salon_id=%s",
            (d['nome'],d.get('tipo','aviso'),d['mensagem'],tid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp/envios', methods=['GET','POST'])
def wpp_envios():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM wpp_envios WHERE salon_id=%s ORDER BY enviado_em DESC LIMIT 200", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO wpp_envios (salon_id,template_id,template_nome,cli_nome,cli_tel,ag_id,mensagem) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (sid,d.get('template_id',0),d.get('template_nome',''),d.get('cli_nome',''),d.get('cli_tel',''),d.get('ag_id',0),d.get('mensagem','')))
    db_commit()
    return jsonify({'ok': True})

# ─── USUÁRIOS ─────────────────────────────────────────────────────────────────
@app.route('/api/usuarios', methods=['GET','POST'])
def usuarios():
    sid, err = require_admin()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT id,nome,login,perfil,permissoes,ativo FROM usuarios WHERE salon_id=%s ORDER BY nome", (sid,), 'all')
        result = []
        for r in rows:
            item = dict(r)
            try: item['permissoes'] = json.loads(item['permissoes'] or '[]')
            except: item['permissoes'] = PERFIS_PADRAO.get(item['perfil'],[])
            result.append(item)
        return jsonify(result)
    d = request.json or {}
    if not d.get('login') or not d.get('senha'):
        return jsonify({'ok':False,'erro':'Login e senha obrigatórios'})
    perfil = d.get('perfil','recepcionista')
    perms  = d.get('permissoes', PERFIS_PADRAO.get(perfil,[]))
    try:
        db_exec("INSERT INTO usuarios (salon_id,nome,login,senha_hash,perfil,permissoes,ativo) VALUES (%s,%s,%s,%s,%s,%s,1)",
                (sid,d.get('nome',''),d['login'],hash_senha(d['senha']),perfil,json.dumps(perms)))
        db_commit()
    except Exception:
        return jsonify({'ok':False,'erro':'Login já existe neste salão'})
    return jsonify({'ok': True})

@app.route('/api/usuarios/<int:uid>', methods=['PUT','DELETE'])
def usuario(uid):
    sid, err = require_admin()
    if err: return err
    if request.method == 'DELETE':
        db_exec("UPDATE usuarios SET ativo=0 WHERE id=%s AND salon_id=%s", (uid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json or {}
    perfil = d.get('perfil','recepcionista')
    perms  = d.get('permissoes', PERFIS_PADRAO.get(perfil,[]))
    if d.get('senha'):
        db_exec("UPDATE usuarios SET nome=%s,perfil=%s,permissoes=%s,ativo=%s,senha_hash=%s WHERE id=%s AND salon_id=%s",
                (d.get('nome',''),perfil,json.dumps(perms),d.get('ativo',1),hash_senha(d['senha']),uid,sid))
    else:
        db_exec("UPDATE usuarios SET nome=%s,perfil=%s,permissoes=%s,ativo=%s WHERE id=%s AND salon_id=%s",
                (d.get('nome',''),perfil,json.dumps(perms),d.get('ativo',1),uid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── OCORRÊNCIAS ──────────────────────────────────────────────────────────────
@app.route('/api/ocorrencias', methods=['GET','POST'])
def ocorrencias():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        cli_id = request.args.get('cli_id','')
        q = "SELECT o.*,c.nome as cli_nome,p.nome as pro_nome FROM ocorrencias o LEFT JOIN clientes c ON c.id=o.cli_id LEFT JOIN profissionais p ON p.id=o.pro_id WHERE o.salon_id=%s"
        params = [sid]
        if cli_id: q += " AND o.cli_id=%s"; params.append(cli_id)
        q += " ORDER BY o.data DESC"
        rows = db_exec(q, params, 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO ocorrencias (salon_id,cli_id,pro_id,data,tipo,descricao,resolvido) VALUES (%s,%s,%s,%s,%s,%s,0)",
            (sid,d.get('cli_id'),d.get('pro_id'),d.get('data',today_br().isoformat()),d.get('tipo',''),d.get('descricao','')))
    db_commit()
    return jsonify({'ok': True})

# ─── AVALIAÇÕES ───────────────────────────────────────────────────────────────
@app.route('/api/avaliacoes', methods=['GET','POST'])
def avaliacoes():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("""SELECT av.*,c.nome as cli_nome,
            COALESCE(p.nome,'') as pro_nome
            FROM avaliacoes av
            LEFT JOIN clientes c ON c.id=av.cli_id
            LEFT JOIN agendamentos a ON a.id=av.ag_id
            LEFT JOIN profissionais p ON p.id=a.pro_id
            WHERE av.salon_id=%s ORDER BY av.data DESC""", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO avaliacoes (salon_id,cli_id,ag_id,nota,comentario,data) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid,d.get('cli_id'),d.get('ag_id',0),d.get('nota',5),d.get('comentario',''),d.get('data',today_br().isoformat())))
    db_commit()
    return jsonify({'ok': True})

# ─── VAREJO ───────────────────────────────────────────────────────────────────
@app.route('/api/varejo', methods=['GET','POST'])
def varejo():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        ini = request.args.get('ini', today_br().replace(day=1).isoformat())
        fim = request.args.get('fim', today_br().isoformat())
        rows = db_exec("SELECT v.*,c.nome as cli_nome FROM varejo v LEFT JOIN clientes c ON c.id=v.cli_id WHERE v.salon_id=%s AND v.data>=%s AND v.data<=%s ORDER BY v.data DESC", (sid,ini,fim), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    total = float(d.get('quantidade',1)) * float(d.get('preco_unit',0))
    db_exec("INSERT INTO varejo (salon_id,data,cli_id,produto,quantidade,preco_unit,total,pag) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (sid,d.get('data',today_br().isoformat()),d.get('cli_id',0),d.get('produto',''),d.get('quantidade',1),d.get('preco_unit',0),total,d.get('pag','')))
    db_commit()
    return jsonify({'ok': True})

# ─── LIXEIRA ──────────────────────────────────────────────────────────────────
@app.route('/api/lixeira', methods=['GET'])
def lixeira():
    sid, err = require_salon()
    if err: return err
    rows = db_exec("SELECT * FROM lixeira_agendamentos WHERE salon_id=%s ORDER BY excluido_em DESC LIMIT 200", (sid,), 'all')
    return jsonify([dict(r) for r in rows])

# ─── FILA DE ESPERA ───────────────────────────────────────────────────────────
@app.route('/api/fila-espera', methods=['GET','POST'])
def fila_espera():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("""SELECT f.*,c.nome as cli_nome,c.tel as cli_tel,p.nome as pro_nome,s.nome as svc_nome
            FROM fila_espera f LEFT JOIN clientes c ON c.id=f.cli_id LEFT JOIN profissionais p ON p.id=f.pro_id
            LEFT JOIN servicos s ON s.id=f.svc_id WHERE f.salon_id=%s AND f.atendido=0 ORDER BY f.criado_em""", (sid,), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO fila_espera (salon_id,cli_id,pro_id,svc_id,data_preferencia,obs) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid,d.get('cli_id'),d.get('pro_id',0),d.get('svc_id',0),d.get('data_preferencia',''),d.get('obs','')))
    db_commit()
    return jsonify({'ok': True})

# ─── METAS ────────────────────────────────────────────────────────────────────
@app.route('/api/metas', methods=['GET','POST'])
def metas():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        mes = request.args.get('mes', today_br().strftime('%Y-%m'))
        rows = db_exec("""SELECT m.*,p.nome as pro_nome,
            COALESCE((SELECT SUM(c.valor) FROM comissoes c JOIN agendamentos a ON a.id=c.ag_id WHERE c.pro_id=m.pro_id AND c.salon_id=m.salon_id AND a.data LIKE m.mes||'%%'),0) as realizado
            FROM metas m LEFT JOIN profissionais p ON p.id=m.pro_id WHERE m.salon_id=%s AND m.mes=%s""", (sid,mes), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO metas (salon_id,pro_id,mes,valor_meta) VALUES (%s,%s,%s,%s) ON CONFLICT (salon_id,pro_id,mes) DO UPDATE SET valor_meta=%s",
            (sid,d['pro_id'],d['mes'],d.get('valor_meta',0),d.get('valor_meta',0)))
    db_commit()
    return jsonify({'ok': True})

# ─── ANIVERSARIANTES ──────────────────────────────────────────────────────────
@app.route('/api/aniversariantes', methods=['GET'])
def aniversariantes():
    sid, err = require_salon()
    if err: return err
    mes = request.args.get('mes', str(today_br().month).zfill(2))
    rows = db_exec("SELECT * FROM clientes WHERE salon_id=%s AND ativo=1 AND nasc!='' AND nasc IS NOT NULL AND SUBSTRING(nasc,6,2)=%s ORDER BY SUBSTRING(nasc,9,2)", (sid,mes), 'all')
    return jsonify([dict(r) for r in rows])

# ─── PRO SVC CONFIG ───────────────────────────────────────────────────────────
@app.route('/api/pro-svc-config/<int:pro_id>', methods=['GET','POST'])
def pro_svc_config(pro_id):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        rows = db_exec("SELECT * FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s", (sid,pro_id), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json or {}
    configs = d.get('configs', [])
    db_exec("DELETE FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s", (sid,pro_id))
    for cfg in configs:
        db_exec("INSERT INTO pro_svc_config (salon_id,pro_id,categoria,svc_id,comissao_override) VALUES (%s,%s,%s,%s,%s)",
                (sid,pro_id,cfg.get('categoria',''),cfg.get('svc_id',0),cfg.get('comissao_override',-1)))
    db_commit()
    return jsonify({'ok': True})

# ─── INDISPONIBILIDADES ───────────────────────────────────────────────────────
@app.route('/api/indisponibilidades', methods=['GET','POST'])
def indisponibilidades():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        pro_id = request.args.get('pro_id','')
        q = "SELECT * FROM indisponibilidades WHERE salon_id=%s"
        params = [sid]
        if pro_id: q += " AND pro_id=%s"; params.append(pro_id)
        rows = db_exec(q, params, 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO indisponibilidades (salon_id,pro_id,data_inicio,data_fim,motivo) VALUES (%s,%s,%s,%s,%s)",
            (sid,d['pro_id'],d['data_inicio'],d.get('data_fim',d['data_inicio']),d.get('motivo','')))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/indisponibilidades/<int:iid>', methods=['DELETE'])
def indisponibilidade_del(iid):
    sid, err = require_salon()
    if err: return err
    db_exec("DELETE FROM indisponibilidades WHERE id=%s AND salon_id=%s", (iid,sid))
    db_commit()
    return jsonify({'ok': True})

# ─── PRÓXIMO HORÁRIO ──────────────────────────────────────────────────────────
@app.route('/api/agendamentos/proximo-horario', methods=['POST'])
def proximo_horario():
    sid, err = require_salon()
    if err: return err
    d = request.json
    pro_id  = d.get('pro_id')
    data    = d.get('data')
    h_ini   = d.get('h_ini')
    duracao = int(d.get('duracao_min', 60))
    ags = db_exec("SELECT h_ini,h_fim FROM agendamentos WHERE salon_id=%s AND pro_id=%s AND data=%s AND status!='cancelado' ORDER BY h_ini",
                  (sid,pro_id,data), 'all')
    def hm(s):
        p = s.split(':'); return int(p[0])*60+int(p[1])
    def mh(m):
        return '%02d:%02d' % (m//60, m%60)
    start    = hm(h_ini)
    occupied = sorted([(hm(a['h_ini']),hm(a['h_fim'])) for a in ags])
    for _ in range(20):
        end = start + duracao
        if not any(s < end and e > start for s,e in occupied):
            return jsonify({'h_ini':mh(start),'h_fim':mh(end),'disponivel':True})
        for s,e in occupied:
            if s < end and e > start:
                start = e; break
    return jsonify({'h_ini':mh(start),'h_fim':mh(start+duracao),'disponivel':False})

# ─── AGENDA FINANCEIRA ────────────────────────────────────────────────────────
@app.route('/api/agenda-financeira', methods=['GET','POST'])
def agenda_fin():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        ini = request.args.get('ini', today_br().replace(day=1).isoformat())
        fim = request.args.get('fim', today_br().isoformat())
        rows = db_exec("SELECT * FROM agenda_financeira WHERE salon_id=%s AND data>=%s AND data<=%s ORDER BY data", (sid,ini,fim), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json
    db_exec("INSERT INTO agenda_financeira (salon_id,data,descricao,valor,tipo,realizado) VALUES (%s,%s,%s,%s,%s,%s)",
            (sid,d.get('data'),d.get('descricao',''),d.get('valor',0),d.get('tipo','receita'),d.get('realizado',0)))
    db_commit()
    return jsonify({'ok': True})



# -- CADASTRO PUBLICO DE SALAO (self-service) ---------------------------------
@app.route('/api/cadastro', methods=['POST'])
def cadastro_salao():
    d = request.json or {}
    nome_salao  = (d.get('nome_salao') or '').strip()
    nome_resp   = (d.get('nome_responsavel') or '').strip()
    email       = (d.get('email') or '').strip().lower()
    senha       = (d.get('senha') or '')
    telefone    = (d.get('telefone') or '').strip()
    if not nome_salao:
        return jsonify({'ok': False, 'erro': 'Nome do salao e obrigatorio'})
    if not email:
        return jsonify({'ok': False, 'erro': 'Email e obrigatorio'})
    if not senha or len(senha) < 6:
        return jsonify({'ok': False, 'erro': 'Senha deve ter no minimo 6 caracteres'})
    ex = db_exec("SELECT id FROM saloes WHERE email=%s", (email,), 'one')
    if ex:
        return jsonify({'ok': False, 'erro': 'Este email ja esta cadastrado'})
    import datetime as _dt2
    trial_fim = (_dt2.date.today() + _dt2.timedelta(days=7)).isoformat()
    cur = db_exec(
        "INSERT INTO saloes (nome,telefone,email,plano,ativo) VALUES (%s,%s,%s,'trial',1) RETURNING id",
        (nome_salao, telefone, email), 'one')
    salon_id = cur['id']
    db_commit()
    seed_novo_salao(salon_id, nome_salao)
    db_exec("DELETE FROM usuarios WHERE salon_id=%s AND login='admin'", (salon_id,))
    db_exec(
        "INSERT INTO usuarios (salon_id,nome,login,telefone,senha_hash,perfil,permissoes,ativo) VALUES (%s,%s,%s,%s,%s,'admin','[]',1)",
        (salon_id, nome_resp or nome_salao, email, telefone, hash_senha(senha)))
    db_exec("UPDATE sistema_config SET valor=%s WHERE salon_id=%s AND chave='usuario_admin'", (email, salon_id))
    db_exec("UPDATE sistema_config SET valor=%s WHERE salon_id=%s AND chave='senha_admin'", (hash_senha(senha), salon_id))
    db_commit()
    u = db_exec("SELECT * FROM usuarios WHERE salon_id=%s AND login=%s", (salon_id, email), 'one')
    session.permanent = True
    session['salon_id']   = salon_id
    session['salon_nome'] = nome_salao
    session['uid']        = u['id']
    session['unome']      = u['nome']
    session['uperfil']    = 'admin'
    return jsonify({
        'ok': True, 'salon_id': salon_id, 'salon_nome': nome_salao,
        'trial_fim': trial_fim, 'nome': u['nome'], 'perfil': 'admin',
        'permissoes': get_permissoes(dict(u))
    })



# ─── SUPER ADMIN — Config Evolution API ─────────────────────────────────────
@app.route('/api/superadmin/evolution', methods=['GET','POST'])
def sa_evolution():
    err = _require_sa()
    if err: return err
    if request.method == 'GET':
        url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
        key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
        return jsonify({
            'evolution_url': url['valor'] if url else '',
            'evolution_apikey': key['valor'] if key else ''
        })
    d = request.json or {}
    db_exec("UPDATE sistema_global SET valor=%s, atualizado_em=NOW() WHERE chave='evolution_url'",
            (d.get('evolution_url',''),))
    db_exec("UPDATE sistema_global SET valor=%s, atualizado_em=NOW() WHERE chave='evolution_apikey'",
            (d.get('evolution_apikey',''),))
    db_commit()
    return jsonify({'ok': True})

# ─── LINK EXCLUSIVO DO SALÃO ─────────────────────────────────────────────────
@app.route('/s/<int:sid>')
def salon_link(sid):
    """Link exclusivo do salão — equipe acessa por aqui."""
    salao = db_exec("SELECT * FROM saloes WHERE id=%s AND ativo=1", (sid,), 'one')
    if not salao:
        return send_from_directory('static', 'entrar.html')
    # Passar dados do salão como parâmetro para o login page
    return send_from_directory('static', 'login.html')

@app.route('/api/salao-info/<int:sid>', methods=['GET'])
def salao_info(sid):
    """Info pública do salão para tela de login da equipe."""
    row = db_exec("SELECT id,nome,telefone,endereco,logo FROM saloes WHERE id=%s AND ativo=1", (sid,), 'one')
    if not row:
        return jsonify({'erro': 'Salão não encontrado'}), 404
    return jsonify(dict(row))

# ─── SALÃO PÚBLICO (sem auth — apenas nome para tela de login) ───────────────
@app.route('/api/salao-publico/<int:sid>', methods=['GET'])
def salao_publico(sid):
    row = db_exec("SELECT id,nome,logo FROM saloes WHERE id=%s AND ativo=1", (sid,), 'one')
    if not row:
        return jsonify({'erro': 'Salão não encontrado'}), 404
    return jsonify({'id': row['id'], 'nome': row['nome'], 'logo': row['logo']})


# ─── ALIASES e ROTAS FALTANDO ────────────────────────────────────────────────

# wpp-templates (frontend usa kebab-case)
@app.route('/api/wpp-templates', methods=['GET','POST'])
def wpp_templates_alias():
    return wpp_templates()

@app.route('/api/wpp-templates/<int:tid>', methods=['PUT','DELETE'])
def wpp_template_alias(tid):
    return wpp_template(tid)

@app.route('/api/wpp-envios', methods=['GET','POST'])
def wpp_envios_alias():
    return wpp_envios()

# comissoes-varejo
@app.route('/api/comissoes-varejo', methods=['GET','POST'])
def comissoes_varejo_list():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        ini = request.args.get('ini', today_br().replace(day=1).isoformat())
        fim = request.args.get('fim', today_br().isoformat())
        pro_id = request.args.get('pro_id','')
        q = "SELECT * FROM comissoes_varejo WHERE salon_id=%s AND data>=%s AND data<=%s"
        params = [sid, ini, fim]
        if pro_id: q += " AND pro_id=%s"; params.append(pro_id)
        q += " ORDER BY data DESC"
        rows = db_exec(q, params, 'all')
        return jsonify([dict(r) for r in rows])
    d = request.json or {}
    db_exec("INSERT INTO comissoes_varejo (salon_id,pro_id,data,produto,cli_nome,valor_venda,com_pct,com_valor,pago,obs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (sid,d.get('pro_id'),d.get('data',today_br().isoformat()),d.get('produto',''),d.get('cli_nome',''),
             d.get('valor_venda',0),d.get('com_pct',0),d.get('com_valor',0),d.get('pago',0),d.get('obs','')))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/comissoes-varejo/<int:cvid>', methods=['PUT','DELETE'])
def comissao_varejo(cvid):
    sid, err = require_salon()
    if err: return err
    if request.method == 'DELETE':
        db_exec("DELETE FROM comissoes_varejo WHERE id=%s AND salon_id=%s", (cvid,sid))
        db_commit()
        return jsonify({'ok': True})
    d = request.json or {}
    db_exec("UPDATE comissoes_varejo SET pago=%s,data_pagamento=%s WHERE id=%s AND salon_id=%s",
            (d.get('pago',0),d.get('data_pagamento',''),cvid,sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/comissoes/pagar-lote', methods=['POST'])
def comissoes_pagar_lote():
    sid, err = require_salon()
    if err: return err
    d = request.json or {}
    ids = d.get('ids', [])
    for cid in ids:
        db_exec("UPDATE comissoes SET pago=1,data_pagamento=%s WHERE id=%s AND salon_id=%s",
                (today_br().isoformat(), cid, sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/comissao-config/<int:pro_id>', methods=['GET','POST'])
def comissao_config(pro_id):
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        # Junta TODOS os serviços ativos com a config do profissional (habilitado ou não)
        rows = db_exec("""
            SELECT s.id AS svc_id, s.nome, s.categoria, s.preco, s.duracao_min,
                   CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END AS habilitado,
                   COALESCE(NULLIF(c.comissao_override, -1), s.comissao_pct, 40) AS comissao_pct
            FROM servicos s
            LEFT JOIN pro_svc_config c
                   ON c.svc_id = s.id AND c.pro_id = %s AND c.salon_id = %s
            WHERE s.salon_id = %s AND s.ativo = 1
            ORDER BY s.categoria, s.nome
        """, (pro_id, sid, sid), 'all')
        return jsonify([dict(r) for r in rows])
    d = request.get_json(silent=True)
    if isinstance(d, list):
        configs = d
    elif isinstance(d, dict):
        configs = d.get('configs', [])
    else:
        configs = []
    db_exec("DELETE FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s", (sid, pro_id))
    for cfg in configs:
        if not cfg.get('habilitado', True):
            continue
        svc_id = cfg.get('svc_id', 0)
        cat = cfg.get('categoria', '')
        if not cat and svc_id:
            srow = db_exec("SELECT categoria FROM servicos WHERE id=%s AND salon_id=%s", (svc_id, sid), 'one')
            cat = (srow['categoria'] if srow else '') or ''
        pct = cfg.get('comissao_pct', cfg.get('comissao_override', -1))
        try:
            pct = float(pct)
        except Exception:
            pct = -1
        db_exec("INSERT INTO pro_svc_config (salon_id,pro_id,categoria,svc_id,comissao_override) VALUES (%s,%s,%s,%s,%s)",
                (sid, pro_id, cat, svc_id, pct))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/pro-svc-config/check', methods=['GET'])
def pro_svc_check():
    sid, err = require_salon()
    if err: return err
    pro_id = request.args.get('pro_id')
    rows = db_exec("SELECT * FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s", (sid,pro_id), 'all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/faturamento/12meses', methods=['GET'])
def fat_12meses():
    return faturamento_mensal()

@app.route('/api/faturamento/5anos', methods=['GET'])
def fat_5anos():
    sid, err = require_salon()
    if err: return err
    anos = []
    hoje = today_br()
    for i in range(4,-1,-1):
        ano = hoje.year - i
        fat = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND EXTRACT(YEAR FROM data::date)=%s AND status='concluido'", (sid,ano), 'one')
        anos.append({'ano':str(ano),'total':round(fat['t'] or 0,2)})
    return jsonify(anos)

@app.route('/api/faturamento/comparativo', methods=['GET'])
def fat_comparativo():
    sid, err = require_salon()
    if err: return err
    hoje = today_br()
    meses = []
    for i in range(11,-1,-1):
        from dateutil.relativedelta import relativedelta
        try:
            d = hoje - relativedelta(months=i)
        except:
            import datetime as _dt3
            d = hoje.replace(day=1) - _dt3.timedelta(days=i*28)
            d = d.replace(day=1)
        ini = d.replace(day=1).isoformat()
        import calendar
        fim = d.replace(day=calendar.monthrange(d.year,d.month)[1]).isoformat()
        fat = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND data<=%s AND status='concluido'", (sid,ini,fim), 'one')
        meses.append({'mes':d.strftime('%Y-%m'),'label':d.strftime('%b/%y'),'total':round(fat['t'] or 0,2)})
    return jsonify(meses)

@app.route('/api/historico-ag/<int:aid>', methods=['GET'])
def historico_ag(aid):
    sid, err = require_salon()
    if err: return err
    rows = db_exec("SELECT * FROM historico_agendamentos WHERE salon_id=%s AND ag_id=%s ORDER BY criado_em DESC", (sid,aid), 'all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/retorno-popup', methods=['GET'])
def retorno_popup():
    sid, err = require_salon()
    if err: return err
    hoje = today_br().isoformat()
    rows = db_exec("""SELECT ra.*,c.nome as cli_nome,c.tel as cli_tel FROM retorno_alertas ra
        JOIN clientes c ON c.id=ra.cli_id WHERE ra.salon_id=%s AND ra.realizado=0 AND ra.data_retorno<=%s
        ORDER BY ra.data_retorno LIMIT 10""", (sid,hoje), 'all')
    return jsonify([dict(r) for r in rows])

@app.route('/api/backup', methods=['GET'])
def backup():
    sid, err = require_salon()
    if err: return err
    return jsonify({'ok': True, 'msg': 'Backup disponível em breve'})

@app.route('/api/exportar/<string:tipo>', methods=['GET'])
def exportar(tipo):
    sid, err = require_salon()
    if err: return err
    return jsonify({'ok': True, 'msg': 'Exportação disponível em breve'})

@app.route('/api/exportar/verificar', methods=['GET'])
def exportar_verificar():
    sid, err = require_salon()
    if err: return err
    return jsonify({'ok': True})

@app.route('/api/importar-salao99', methods=['POST'])
def importar_salao99():
    """Importa CSVs do Salão99 (colaborador, servico, cliente, atendimento)."""
    sid, err = require_salon()
    if err: return err
    try:
        return _importar_salao99_inner(sid)
    except Exception as ex_global:
        db_rollback()
        import traceback
        tb = traceback.format_exc()
        try:
            db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_imperr', %s)
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (str(ex_global)[:300] + ' | ' + tb[-300:],))
            db_commit()
        except Exception:
            pass
        return jsonify({'ok': False, 'erro': str(ex_global), 'traceback': tb[-500:]}), 200

def _importar_salao99_inner(sid):
    import csv as _csv, io as _io, re as _re

    files = request.files
    res = {'colaboradores': 0, 'servicos': 0, 'clientes': 0, 'atendimentos': 0, 'erros': []}

    def read_csv(campo):
        f = files.get(campo)
        if not f:
            return []
        raw = f.read()
        content = None
        for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
            try:
                content = raw.decode(enc); break
            except Exception:
                continue
        if content is None:
            content = raw.decode('utf-8', errors='ignore')
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        sep = ';' if content.count(';') >= content.count(',') else ','
        rows = []
        for r in _csv.DictReader(_io.StringIO(content), delimiter=sep):
            rows.append({(k or '').strip().lstrip('\ufeff'): (v or '').strip() for k, v in r.items()})
        return rows

    def parse_dur(s):
        s = str(s).strip().lower()
        if s.isdigit():
            return int(s) or 60
        h = _re.search(r'(\d+)\s*h', s); m = _re.search(r'(\d+)\s*min', s)
        total = 0
        if h: total += int(h.group(1)) * 60
        if m: total += int(m.group(1))
        return total or 60

    def parse_preco(s):
        try:
            s = str(s).strip()
            if not s: return 0.0
            if ',' in s:
                return float(s.replace('.', '').replace(',', '.'))
            return float(s)
        except Exception:
            return 0.0

    def parse_hora(s):
        s = str(s).strip()
        if ':' in s:
            p = s.split(':')
            return p[0].zfill(2) + ':' + p[1].zfill(2)[:2]
        s = s.zfill(4)
        if len(s) >= 4:
            return s[:2] + ':' + s[2:4]
        return '08:00'

    def parse_data(s):
        s = str(s).strip()
        if len(s) == 8 and s.isdigit():
            return s[:4] + '-' + s[4:6] + '-' + s[6:]
        return s

    pro_map, svc_map, cli_map = {}, {}, {}

    try:
        from psycopg2.extras import execute_values
        conn = get_db()
        cur = conn.cursor()

        # ───── 0. LIMPAR DADOS DE EXEMPLO (seed) ─────
        # Remove clientes/profissionais/agendamentos fictícios criados ao abrir o salão,
        # para não se misturarem com os dados reais importados.
        try:
            cli_exemplo = ('Ana Paula Oliveira', 'Carla Mendes', 'Juliana Ferreira')
            cur.execute("SELECT id FROM clientes WHERE salon_id=%s AND nome IN %s", (sid, cli_exemplo))
            ids_cli_ex = [r['id'] for r in cur.fetchall()]
            if ids_cli_ex:
                cur.execute("DELETE FROM agendamentos WHERE salon_id=%s AND cli_id = ANY(%s)", (sid, ids_cli_ex))
                cur.execute("DELETE FROM clientes WHERE salon_id=%s AND id = ANY(%s)", (sid, ids_cli_ex))
            conn.commit()
        except Exception:
            conn.rollback()

        # ───── 1. COLABORADORES ─────
        rows = read_csv('colaborador')
        if rows:
            cur.execute("SELECT id,nome FROM profissionais WHERE salon_id=%s", (sid,))
            existe = {r['nome'].lower(): r['id'] for r in cur.fetchall()}
            novos = []
            for row in rows:
                nome = (row.get('Nome') or row.get('nome') or '').strip()
                if not nome or nome.lower() in existe: continue
                com = parse_preco(row.get('comissao_pct') or row.get('comissao') or 40) or 40
                novos.append((sid, nome, row.get('cargo','') or '', row.get('cor','#EC4899') or '#EC4899',
                              com, row.get('h_inicio','08:00') or '08:00', row.get('h_fim','20:00') or '20:00', 1))
            if novos:
                execute_values(cur,
                    "INSERT INTO profissionais (salon_id,nome,cargo,cor,comissao_pct,h_inicio,h_fim,ativo) VALUES %s",
                    novos, page_size=500)
                conn.commit()
                res['colaboradores'] = len(novos)

        # ───── 2. SERVIÇOS ─────
        rows = read_csv('servico')
        if rows:
            cur.execute("SELECT id,nome FROM servicos WHERE salon_id=%s", (sid,))
            existe = {r['nome'].lower(): r['id'] for r in cur.fetchall()}
            novos = []
            for row in rows:
                nome = (row.get('Servico') or row.get('servico') or row.get('nome') or '').strip()
                if not nome or nome.lower() in existe: continue
                cat = (row.get('Categoria') or row.get('categoria') or '').strip()
                dur = parse_dur(row.get('Duracao') or row.get('duracao_min') or row.get('duracao') or '60')
                preco = parse_preco(row.get('Preco (R$)') or row.get('preco') or '0')
                com = parse_preco(row.get('comissao_pct') or row.get('Comissao Sozinho') or row.get('comissao') or '40') or 40
                novos.append((sid, nome, cat, dur, preco, com, 1))
            if novos:
                execute_values(cur,
                    "INSERT INTO servicos (salon_id,nome,categoria,duracao_min,preco,comissao_pct,ativo) VALUES %s",
                    novos, page_size=500)
                conn.commit()
                res['servicos'] = len(novos)

        # ───── 3. CLIENTES ─────
        rows = read_csv('cliente')
        if rows:
            cur.execute("SELECT id,nome,tel FROM clientes WHERE salon_id=%s", (sid,))
            existe_nome = {}; existe_tel = {}
            for r in cur.fetchall():
                existe_nome[r['nome'].lower()] = r['id']
                if r['tel']: existe_tel[r['tel']] = r['id']
            novos = []; vistos = set()
            hoje_iso = today_br().isoformat()
            for row in rows:
                nome = (row.get('nome') or row.get('Nome') or '').strip()
                if not nome: continue
                tel = (row.get('tel') or row.get('telefone_1') or row.get('celular') or row.get('telefone') or '').strip()
                chave = nome.lower()
                if chave in existe_nome or chave in vistos: continue
                if tel and tel in existe_tel: continue
                vistos.add(chave)
                novos.append((sid, nome, tel, row.get('email','') or '', row.get('cpf','') or '',
                              row.get('nasc','') or '', 1, hoje_iso))
            if novos:
                execute_values(cur,
                    "INSERT INTO clientes (salon_id,nome,tel,email,cpf,nasc,ativo,criado_em) VALUES %s",
                    novos, page_size=500)
                conn.commit()
                res['clientes'] = len(novos)

        # ───── 4. ATENDIMENTOS ─────
        rows = read_csv('atendimento')
        if rows:
            cur.execute("SELECT id,nome FROM profissionais WHERE salon_id=%s", (sid,))
            for r in cur.fetchall(): pro_map[r['nome'].strip().lower()] = r['id']
            cur.execute("SELECT id,nome FROM servicos WHERE salon_id=%s", (sid,))
            for r in cur.fetchall(): svc_map[r['nome'].strip().lower()] = r['id']
            cur.execute("SELECT id,nome FROM clientes WHERE salon_id=%s", (sid,))
            for r in cur.fetchall(): cli_map[r['nome'].strip().upper()] = r['id']
            fb_pro_id = next(iter(pro_map.values()), 0)
            fb_svc_id = next(iter(svc_map.values()), 0)
            cur.execute("SELECT cli_id,data,h_ini FROM agendamentos WHERE salon_id=%s", (sid,))
            existentes = set((r['cli_id'], r['data'], r['h_ini']) for r in cur.fetchall())

            registros = []
            for i, row in enumerate(rows):
                try:
                    cli_nome = (row.get('cliente') or '').strip().upper()
                    data = parse_data((row.get('data', '') or '').strip())
                    if not data or len(data) != 10: continue
                    col_nome = (row.get('colaborador') or '').strip().lower()
                    svc_nome = (row.get('servico') or '').strip().lower()
                    h_ini = parse_hora(row.get('horario_inicio') or row.get('hora_inicio') or '0800')
                    h_fim = parse_hora(row.get('horario_termino') or row.get('hora_fim') or h_ini)
                    preco = parse_preco(row.get('valor_total') or row.get('valor_subtotal') or row.get('valor') or '0')
                    status_raw = (row.get('status') or 'concluido').strip().lower()
                    if 'conclu' in status_raw: status = 'concluido'
                    elif 'cancel' in status_raw or 'faltou' in status_raw: status = 'cancelado'
                    elif 'confirm' in status_raw: status = 'confirmado'
                    else: status = 'agendado'
                    cli_id = cli_map.get(cli_nome, 0)
                    if not cli_id: continue
                    pro_id = pro_map.get(col_nome)
                    if not pro_id:
                        primeiro = col_nome.split()[0] if col_nome else ''
                        for k, v in pro_map.items():
                            if primeiro and primeiro in k: pro_id = v; break
                        if not pro_id: pro_id = fb_pro_id
                    svc_id = svc_map.get(svc_nome)
                    if not svc_id:
                        for k, v in svc_map.items():
                            if svc_nome and svc_nome in k: svc_id = v; break
                        if not svc_id: svc_id = fb_svc_id
                    if not pro_id or not svc_id: continue
                    chave = (cli_id, data, h_ini)
                    if chave in existentes: continue
                    existentes.add(chave)
                    registros.append((sid, cli_id, pro_id, svc_id, data, h_ini, h_fim, preco, status))
                except Exception as e:
                    if len(res['erros']) < 10:
                        res['erros'].append('Linha ' + str(i+2) + ': ' + str(e)[:60])
            if registros:
                execute_values(cur,
                    "INSERT INTO agendamentos (salon_id,cli_id,pro_id,svc_id,data,h_ini,h_fim,preco,status) VALUES %s",
                    registros, page_size=1000)
                conn.commit()
                res['atendimentos'] = len(registros)

            # ───── 5. RELACIONAR PROFISSIONAIS ↔ SERVIÇOS (a partir dos atendimentos) ─────
            # Extra: descobre serviços que cada profissional realizou e habilita na ficha.
            # Roda isolado: se falhar, NÃO afeta a importação (que já foi commitada).
            try:
                import json as _json
                cur.execute("SELECT id,categoria,comissao_pct FROM servicos WHERE salon_id=%s", (sid,))
                svc_info = {r['id']: {'cat': r['categoria'] or '', 'pct': r['comissao_pct'] or 40} for r in cur.fetchall()}
                cur.execute("SELECT DISTINCT pro_id, svc_id FROM agendamentos WHERE salon_id=%s AND pro_id>0 AND svc_id>0", (sid,))
                pro_svcs = {}
                for r in cur.fetchall():
                    pro_svcs.setdefault(r['pro_id'], set()).add(r['svc_id'])
                conn.commit()

                for pro_id, svc_ids in pro_svcs.items():
                    try:
                        cats = sorted(set(svc_info[s]['cat'] for s in svc_ids if s in svc_info and svc_info[s]['cat']))
                        if cats:
                            cur.execute("UPDATE profissionais SET categorias=%s WHERE id=%s AND salon_id=%s",
                                        (_json.dumps(cats, ensure_ascii=False), pro_id, sid))
                        cur.execute("DELETE FROM pro_svc_config WHERE salon_id=%s AND pro_id=%s", (sid, pro_id))
                        for s in svc_ids:
                            if s not in svc_info: continue
                            cur.execute("""INSERT INTO pro_svc_config (salon_id,pro_id,categoria,svc_id,comissao_override)
                                           VALUES (%s,%s,%s,%s,%s)
                                           ON CONFLICT (salon_id,pro_id,svc_id) DO NOTHING""",
                                        (sid, pro_id, svc_info[s]['cat'], s, svc_info[s]['pct']))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            except Exception as e_rel:
                try: conn.rollback()
                except Exception: pass
                if len(res['erros']) < 10:
                    res['erros'].append('Relacao pro-svc: ' + str(e_rel)[:80])

    except Exception as ex:
        try: conn.rollback()
        except Exception: pass
        import traceback
        try:
            db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_imperr', %s)
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (str(ex)[:200] + ' | ' + traceback.format_exc()[-200:],))
            db_commit()
        except Exception: pass
        return jsonify({'ok': False, 'erro': str(ex), 'traceback': traceback.format_exc()[-400:], 'parcial': res})

    return jsonify({'ok': True, 'resultado': res})

@app.route('/api/bloqueios', methods=['GET','POST'])
def bloqueios():
    return indisponibilidades()

@app.route('/api/agendamentos/recorrente', methods=['POST'])
def ag_recorrente():
    sid, err = require_salon()
    if err: return err
    return jsonify({'ok': False, 'erro': 'Agendamento recorrente em breve'})

@app.route('/api/auth/recuperar', methods=['POST'])
def auth_recuperar():
    """Gera código de 6 dígitos e envia por email ou WhatsApp."""
    d = request.json or {}
    login_val = (d.get('login') or '').strip()
    via = (d.get('via') or 'email').strip()  # 'email' ou 'whatsapp'
    if not login_val:
        return jsonify({'ok': False, 'erro': 'Informe seu email ou telefone.'})

    so_dig = ''.join(ch for ch in login_val if ch.isdigit())
    # Achar usuário por email ou telefone
    u = db_exec("""SELECT u.*, s.nome as salao_nome FROM usuarios u
                   LEFT JOIN saloes s ON s.id=u.salon_id
                   WHERE u.ativo=1 AND (u.login=LOWER(%s) OR
                         (u.telefone!='' AND regexp_replace(u.telefone,'[^0-9]','','g')=%s))
                   LIMIT 1""", (login_val, so_dig), 'one')
    if not u:
        # Resposta neutra (não revela se existe) mas sem travar
        return jsonify({'ok': True, 'msg': 'Se os dados estiverem corretos, você receberá um código.'})

    import random
    codigo = '%06d' % random.randint(0, 999999)
    # Salva código com validade de 15 min
    db_exec("""INSERT INTO sistema_global (chave, valor) VALUES (%s, %s)
               ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor, atualizado_em=NOW()""",
            ('recsenha_' + str(u['id']), codigo + '|' + (now_br() + datetime.timedelta(minutes=15)).isoformat()))
    db_commit()

    salao_nome = u['salao_nome'] or 'Musa'
    texto = ('Olá! Seu código de recuperação de senha do ' + salao_nome + ' é: ' + codigo +
             '\n\nVálido por 15 minutos. Se não foi você, ignore esta mensagem.')

    enviado = False
    if via == 'whatsapp':
        tel = u['telefone'] or ''
        if tel:
            try:
                enviado = _enviar_wpp(u['salon_id'], tel, texto)
            except Exception:
                enviado = False
        if not enviado:
            return jsonify({'ok': False, 'erro': 'Não foi possível enviar pelo WhatsApp. Verifique se há telefone cadastrado.'})
    else:
        # Email
        ok_mail = _enviar_email(u['login'], 'Código de recuperação - ' + salao_nome, texto)
        if not ok_mail:
            return jsonify({'ok': False, 'erro': 'Não foi possível enviar o email no momento.'})
        enviado = True

    destino = (u['telefone'][-4:] if via == 'whatsapp' and u['telefone'] else (u['login'][:3] + '***'))
    return jsonify({'ok': True, 'user_id': u['id'], 'msg': 'Código enviado!', 'destino': destino})

@app.route('/api/auth/resetar', methods=['POST'])
def auth_resetar():
    """Valida o código e define a nova senha."""
    d = request.json or {}
    user_id = d.get('user_id')
    codigo = (d.get('codigo') or '').strip()
    nova = (d.get('nova_senha') or '').strip()
    if not (user_id and codigo and nova):
        return jsonify({'ok': False, 'erro': 'Preencha o código e a nova senha.'})
    if len(nova) < 4:
        return jsonify({'ok': False, 'erro': 'A senha deve ter ao menos 4 caracteres.'})

    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('recsenha_' + str(user_id),), 'one')
    if not row or '|' not in (row['valor'] or ''):
        return jsonify({'ok': False, 'erro': 'Código inválido ou expirado. Solicite um novo.'})
    cod_salvo, validade = row['valor'].split('|', 1)
    try:
        if now_br() > datetime.datetime.fromisoformat(validade):
            return jsonify({'ok': False, 'erro': 'Código expirado. Solicite um novo.'})
    except Exception:
        pass
    if codigo != cod_salvo:
        return jsonify({'ok': False, 'erro': 'Código incorreto.'})

    db_exec("UPDATE usuarios SET senha_hash=%s WHERE id=%s", (hash_senha(nova), user_id))
    db_exec("DELETE FROM sistema_global WHERE chave=%s", ('recsenha_' + str(user_id),))
    db_commit()
    return jsonify({'ok': True, 'msg': 'Senha alterada! Já pode entrar com a nova senha.'})


# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP IA — Configuração e Webhook
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/wpp-ia/config', methods=['GET','POST'])
def wpp_ia_config():
    sid, err = require_salon()
    if err: return err
    if request.method == 'GET':
        row = db_exec("SELECT * FROM wpp_ia_config WHERE salon_id=%s", (sid,), 'one')
        conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
        return jsonify({
            'config': dict(row) if row else {},
            'conexao': dict(conn) if conn else {}
        })
    d = request.json or {}
    modo = d.get('modo_atendimento', '24h')
    # 'ativo' deriva do modo: desativado => 0, senão 1
    ativo_val = 0 if modo == 'desativado' else 1
    # Upsert config IA
    ex = db_exec("SELECT id FROM wpp_ia_config WHERE salon_id=%s", (sid,), 'one')
    if ex:
        db_exec("""UPDATE wpp_ia_config SET ativo=%s,groq_key=%s,saudacao=%s,
                   horario_ini=%s,horario_fim=%s,dias_semana=%s,msg_fora_horario=%s,
                   personalidade=%s,modo_atendimento=%s,atualizado_em=NOW() WHERE salon_id=%s""",
                (ativo_val, d.get('groq_key',''), d.get('saudacao',''),
                 d.get('horario_ini','08:00'), d.get('horario_fim','20:00'),
                 d.get('dias_semana','1,2,3,4,5,6'), d.get('msg_fora_horario',''),
                 d.get('personalidade',''), modo, sid))
    else:
        db_exec("""INSERT INTO wpp_ia_config (salon_id,ativo,groq_key,saudacao,horario_ini,horario_fim,dias_semana,msg_fora_horario,personalidade,modo_atendimento)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sid, ativo_val, d.get('groq_key',''), d.get('saudacao',''),
                 d.get('horario_ini','08:00'), d.get('horario_fim','20:00'),
                 d.get('dias_semana','1,2,3,4,5,6'), d.get('msg_fora_horario',''),
                 d.get('personalidade',''), modo))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp-ia/conectar', methods=['POST'])
def wpp_ia_conectar():
    sid, err = require_salon()
    if err: return err
    d = request.json or {}
    instance_name = 'musa_' + str(sid)
    # Buscar config global da Evolution API
    evo_url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
    evo_key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
    evolution_url = (evo_url['valor'] if evo_url else '').rstrip('/')
    api_key       = evo_key['valor'] if evo_key else ''
    if not evolution_url:
        return jsonify({'ok': False, 'erro': 'Evolution API não configurada. Contate o administrador.'})

    import urllib.request as _ur, urllib.error as _ue, json as _js

    def _evo_req(path, payload=None, method='POST'):
        data_b = _js.dumps(payload).encode() if payload is not None else None
        r = _ur.Request(evolution_url + path, data=data_b,
                        headers={'Content-Type': 'application/json', 'apikey': api_key},
                        method=method)
        with _ur.urlopen(r, timeout=20) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
            return resp.status, body

    webhook_url = request.host_url.rstrip('/') + '/webhook/wpp/' + str(sid)
    if webhook_url.startswith('http://') and 'onrender.com' in webhook_url:
        webhook_url = webhook_url.replace('http://', 'https://')

    # 1. Deletar instância antiga se existir (evita conflito 403)
    try:
        _evo_req('/instance/delete/' + instance_name, method='DELETE')
    except Exception:
        pass
    try:
        _evo_req('/instance/logout/' + instance_name, method='DELETE')
    except Exception:
        pass

    # 2. Criar instância (formato v2.3.7 — sem webhook embutido; configurado separadamente)
    try:
        payload = {
            'instanceName': instance_name,
            'qrcode': True,
            'integration': 'WHATSAPP-BAILEYS'
        }
        status, body = _evo_req('/instance/create', payload)
        data = _js.loads(body)
    except _ue.HTTPError as he:
        eb = he.read().decode('utf-8', errors='ignore')
        if he.code in (403, 409):
            try:
                status, body = _evo_req('/instance/connect/' + instance_name, method='GET')
                data = _js.loads(body)
            except Exception as ex2:
                return jsonify({'ok': False, 'erro': 'Erro ao reconectar: ' + str(ex2)})
        else:
            return jsonify({'ok': False, 'erro': 'HTTP ' + str(he.code) + ': ' + eb[:200]})
    except Exception as ex:
        return jsonify({'ok': False, 'erro': 'Não foi possível conectar: ' + str(ex)})

    # Extrair QR (v1.7.4: qrcode.base64 ou qrcode.code)
    qr = ''
    def _extrai_qr(d):
        if not isinstance(d, dict): return ''
        qc = d.get('qrcode', {})
        if isinstance(qc, dict):
            return qc.get('base64', '') or qc.get('code', '')
        if isinstance(qc, str):
            return qc
        return d.get('base64', '') or d.get('code', '')
    qr = _extrai_qr(data)

    # 3. Garantir webhook (formato v2.3.7 — objeto webhook aninhado)
    try:
        wh_payload = {
            'webhook': {
                'enabled': True,
                'url': webhook_url,
                'webhookByEvents': False,
                'webhookBase64': False,
                'events': ['MESSAGES_UPSERT']
            }
        }
        _evo_req('/webhook/set/' + instance_name, wh_payload)
    except Exception as ex_wh:
        print('Aviso: webhook nao configurado:', ex_wh)

    # Se QR vazio, buscar via connect
    if not qr:
        try:
            import time as _t
            _t.sleep(3)
            status, body = _evo_req('/instance/connect/' + instance_name, method='GET')
            qr = _extrai_qr(_js.loads(body))
        except Exception as ex_qr:
            print('Erro ao buscar QR:', ex_qr)

    ex = db_exec("SELECT id FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
    if ex:
        db_exec("UPDATE wpp_conexoes SET evolution_url=%s,instance_key=%s,instance_name=%s,ativo=0 WHERE salon_id=%s",
                (evolution_url, api_key, instance_name, sid))
    else:
        db_exec("INSERT INTO wpp_conexoes (salon_id,evolution_url,instance_key,instance_name) VALUES (%s,%s,%s,%s)",
                (sid, evolution_url, api_key, instance_name))
    db_commit()
    return jsonify({'ok': True, 'qr': qr, 'instance': instance_name, 'webhook': webhook_url})





@app.route('/api/wpp-ia/debug-num', methods=['GET'])
def wpp_debug_num():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('last_num_'+str(sid),), 'one')
    return jsonify({'info': row['valor'] if row else 'nenhum'})

@app.route('/api/wpp-ia/debug-hist', methods=['GET'])
def wpp_debug_hist():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('last_hist_'+str(sid),), 'one')
    return jsonify({'info': row['valor'] if row else 'nenhum'})

@app.route('/api/wpp-ia/debug-iaerr', methods=['GET'])
def wpp_debug_iaerr():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('last_iaerr_'+str(sid),), 'one')
    return jsonify({'info': row['valor'] if row else 'nenhum erro registrado'})

@app.route('/api/debug-imperr', methods=['GET'])
def debug_imperr():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave='last_imperr'", fetch='one')
    return jsonify({'info': row['valor'] if row else 'nenhum erro registrado'})

@app.route('/api/teste-upload', methods=['POST'])
def teste_upload():
    """Diagnóstico: recebe os arquivos e só conta as linhas, SEM tocar no banco.
    Isola se o problema é o upload ou o processamento no banco."""
    try:
        sid, err = require_salon()
        out = {'salon_id_ok': bool(sid)}
        for campo in ('colaborador', 'servico', 'cliente', 'atendimento'):
            f = request.files.get(campo)
            if f:
                raw = f.read()
                try:
                    txt = raw.decode('utf-8-sig', errors='ignore')
                except Exception:
                    txt = raw.decode('latin-1', errors='ignore')
                n_linhas = len([l for l in txt.split('\n') if l.strip()])
                out[campo] = {'bytes': len(raw), 'linhas': n_linhas}
            else:
                out[campo] = None
        return jsonify({'ok': True, 'recebido': out})
    except Exception as ex:
        import traceback
        return jsonify({'ok': False, 'erro': str(ex), 'tb': traceback.format_exc()[-300:]})

@app.route('/api/wpp-ia/debug-envio', methods=['GET'])
def wpp_debug_envio():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('last_send_'+str(sid),), 'one')
    if not row:
        return jsonify({'tentou_enviar': False, 'msg': 'Nenhuma tentativa de envio registrada ainda.'})
    return jsonify({'tentou_enviar': True, 'ultimo_envio': row['valor']})

@app.route('/api/wpp-ia/debug-webhook', methods=['GET'])
def wpp_debug_webhook():
    sid, err = require_salon()
    if err: return err
    row = db_exec("SELECT valor FROM sistema_global WHERE chave=%s", ('last_webhook_'+str(sid),), 'one')
    if not row:
        return jsonify({'recebido': False, 'msg': 'Nenhum webhook recebido ainda. A Evolution não está enviando mensagens para o sistema.'})
    import json as _j
    try:
        return jsonify({'recebido': True, 'ultimo_webhook': _j.loads(row['valor'])})
    except:
        return jsonify({'recebido': True, 'ultimo_webhook_raw': row['valor']})

@app.route('/api/wpp-ia/reconfig-webhook', methods=['POST'])
def wpp_reconfig_webhook():
    sid, err = require_salon()
    if err: return err
    conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
    if not conn:
        return jsonify({'ok': False, 'erro': 'WhatsApp não conectado'})
    evo_url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
    evo_key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
    evolution_url = (evo_url['valor'] if evo_url else conn['evolution_url']).rstrip('/')
    api_key       = evo_key['valor'] if evo_key else conn['instance_key']
    instance      = conn['instance_name']
    webhook_url = request.host_url.rstrip('/') + '/webhook/wpp/' + str(sid)
    if webhook_url.startswith('http://') and 'onrender.com' in webhook_url:
        webhook_url = webhook_url.replace('http://', 'https://')
    import urllib.request as _ur, json as _js
    try:
        wh_payload = _js.dumps({
            'url': webhook_url,
            'webhook_by_events': False,
            'webhook_base64': False,
            'events': ['MESSAGES_UPSERT']
        }).encode()
        wh_req = _ur.Request(
            evolution_url + '/webhook/set/' + instance,
            data=wh_payload,
            headers={'Content-Type': 'application/json', 'apikey': api_key},
            method='POST'
        )
        with _ur.urlopen(wh_req, timeout=10) as resp:
            result = _js.loads(resp.read())
        return jsonify({'ok': True, 'webhook': webhook_url, 'resultado': result})
    except Exception as ex:
        return jsonify({'ok': False, 'erro': str(ex), 'webhook': webhook_url})

@app.route('/api/wpp-ia/status', methods=['GET'])
def wpp_ia_status():
    sid, err = require_salon()
    if err: return err
    conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
    if not conn:
        return jsonify({'status': 'desconectado'})
    evo_url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
    evo_key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
    evolution_url = (evo_url['valor'] if evo_url else conn.get('evolution_url','')).rstrip('/')
    api_key       = evo_key['valor'] if evo_key else conn.get('instance_key','')
    instance      = conn['instance_name']
    try:
        import urllib.request as _ur, json as _js
        req = _ur.Request(
            evolution_url + '/instance/connectionState/' + instance,
            headers={'apikey': api_key}
        )
        with _ur.urlopen(req, timeout=8) as resp:
            data = _js.loads(resp.read())
        state = data.get('instance', {}).get('state', 'unknown')
        if state == 'open':
            numero = data.get('instance', {}).get('profileName', '') or ''
            db_exec("UPDATE wpp_conexoes SET ativo=1,numero=%s WHERE salon_id=%s", (numero, sid))
            db_commit()
        return jsonify({'status': state, 'numero': conn.get('numero','')})
    except Exception as ex:
        return jsonify({'status': 'erro', 'detalhe': str(ex)})

@app.route('/api/wpp-ia/desconectar', methods=['POST'])
def wpp_ia_desconectar():
    sid, err = require_salon()
    if err: return err
    conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
    if conn:
        import urllib.request as _ur
        evo_url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
        evo_key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
        eurl = (evo_url['valor'] if evo_url else conn['evolution_url']).rstrip('/')
        ekey = evo_key['valor'] if evo_key else conn['instance_key']
        inst = conn['instance_name']
        # Logout E delete (para poder reconectar depois)
        for path in ['/instance/logout/' + inst, '/instance/delete/' + inst]:
            try:
                req = _ur.Request(eurl + path, headers={'apikey': ekey}, method='DELETE')
                _ur.urlopen(req, timeout=8)
            except: pass
        db_exec("UPDATE wpp_conexoes SET ativo=0 WHERE salon_id=%s", (sid,))
        db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp-ia/conversas', methods=['GET'])
def wpp_ia_conversas():
    sid, err = require_salon()
    if err: return err
    rows = db_exec("""
        SELECT numero_cliente, nome_cliente,
               MAX(criado_em) as ultima_msg,
               COUNT(*) as total_msgs
        FROM wpp_conversas WHERE salon_id=%s
        GROUP BY numero_cliente, nome_cliente
        ORDER BY ultima_msg DESC LIMIT 50
    """, (sid,), 'all')
    out = []
    for r in (rows or []):
        d = dict(r)
        # Estado da IA nesta conversa (pausada manual, pausada auto, ou ativa)
        p = db_exec("""SELECT pausado_manual, (pausado_ate > NOW()) as auto_ativa
                       FROM wpp_ia_pausa WHERE salon_id=%s AND numero_cliente=%s""",
                    (sid, (d.get('numero_cliente') or '')[-12:]), 'one')
        if p and p.get('pausado_manual') == 1:
            d['ia_estado'] = 'assumida'
        elif p and p.get('auto_ativa'):
            d['ia_estado'] = 'pausa_auto'
        else:
            d['ia_estado'] = 'ativa'
        out.append(d)
    return jsonify(out)

@app.route('/api/wpp-ia/assumir/<numero>', methods=['POST'])
def wpp_ia_assumir(numero):
    """Assume manualmente a conversa: a IA para de responder este cliente até você devolver."""
    sid, err = require_salon()
    if err: return err
    num = ''.join(ch for ch in numero if ch.isdigit())[-12:]
    db_exec("""INSERT INTO wpp_ia_pausa (salon_id,numero_cliente,pausado_manual,atualizado_em)
               VALUES (%s,%s,1,NOW())
               ON CONFLICT (salon_id,numero_cliente)
               DO UPDATE SET pausado_manual=1, atualizado_em=NOW()""", (sid, num))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp-ia/devolver/<numero>', methods=['POST'])
def wpp_ia_devolver(numero):
    """Devolve a conversa para a IA: ela volta a responder este cliente."""
    sid, err = require_salon()
    if err: return err
    num = ''.join(ch for ch in numero if ch.isdigit())[-12:]
    db_exec("""UPDATE wpp_ia_pausa SET pausado_manual=0, pausado_ate=NULL, atualizado_em=NOW()
               WHERE salon_id=%s AND numero_cliente=%s""", (sid, num))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp-ia/conversa/<numero>', methods=['GET'])
def wpp_ia_conversa(numero):
    sid, err = require_salon()
    if err: return err
    rows = db_exec("""SELECT role,content,criado_em FROM wpp_conversas
        WHERE salon_id=%s AND numero_cliente=%s
        ORDER BY criado_em DESC LIMIT 40""", (sid, numero), 'all')
    return jsonify([dict(r) for r in reversed(rows)])

@app.route('/api/wpp-ia/conversa/<numero>/limpar', methods=['POST'])
def wpp_ia_conversa_limpar(numero):
    sid, err = require_salon()
    if err: return err
    # Limpa por número exato e também por variações (últimos 8 dígitos)
    so_dig = ''.join(ch for ch in numero if ch.isdigit())
    db_exec("DELETE FROM wpp_conversas WHERE salon_id=%s AND numero_cliente LIKE %s",
            (sid, '%' + so_dig[-8:] + '%'))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/wpp-ia/testar', methods=['POST'])
def wpp_ia_testar():
    sid, err = require_salon()
    if err: return err
    d = request.json or {}
    mensagem = d.get('mensagem', 'Olá, quero agendar um horário')
    cfg = db_exec("SELECT * FROM wpp_ia_config WHERE salon_id=%s", (sid,), 'one')
    if not cfg or not cfg.get('groq_key'):
        return jsonify({'ok': False, 'erro': 'Configure sua chave Groq primeiro'})

    salao = db_exec("SELECT * FROM saloes WHERE id=%s", (sid,), 'one')
    pros  = db_exec("SELECT id,nome,cargo,ativo FROM profissionais WHERE salon_id=%s AND ativo=1", (sid,), 'all')
    svcs  = db_exec("SELECT id,nome,preco,duracao_min,ativo FROM servicos WHERE salon_id=%s AND ativo=1", (sid,), 'all')

    try:
        from ia_wpp import responder as _resp
        resposta, cmds, erro = _resp(
            mensagem,
            dict(salao) if salao else {},
            [dict(p) for p in pros],
            [dict(s) for s in svcs],
            None, None,
            cfg['groq_key'],
            cfg.get('personalidade', '')
        )
        if erro:
            return jsonify({'ok': False, 'erro': erro})
        return jsonify({'ok': True, 'resposta': resposta, 'comandos': cmds})
    except Exception as ex:
        return jsonify({'ok': False, 'erro': str(ex)})

# ─── CONTATO AUTOMÁTICO (Retorno e Inativos) ──────────────────────────────────
def _enviar_email(destinatario, assunto, corpo):
    """Envia email via SMTP. Lê config de sistema_global (smtp_host, smtp_port, smtp_user, smtp_pass).
    Retorna True/False. Se SMTP não estiver configurado, retorna False."""
    try:
        host = db_exec("SELECT valor FROM sistema_global WHERE chave='smtp_host'", fetch='one')
        user = db_exec("SELECT valor FROM sistema_global WHERE chave='smtp_user'", fetch='one')
        pwd  = db_exec("SELECT valor FROM sistema_global WHERE chave='smtp_pass'", fetch='one')
        port = db_exec("SELECT valor FROM sistema_global WHERE chave='smtp_port'", fetch='one')
        host = host['valor'] if host else ''
        user = user['valor'] if user else ''
        pwd  = pwd['valor'] if pwd else ''
        port = int(port['valor']) if port and port['valor'] else 587
        if not (host and user and pwd):
            return False
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(corpo, 'plain', 'utf-8')
        msg['Subject'] = assunto
        msg['From'] = user
        msg['To'] = destinatario
        with smtplib.SMTP(host, port, timeout=15) as srv:
            srv.starttls()
            srv.login(user, pwd)
            srv.sendmail(user, [destinatario], msg.as_string())
        return True
    except Exception as ex:
        print('Erro enviar email:', ex)
        return False

def _enviar_wpp(sid, numero, texto):
    """Envia uma mensagem de WhatsApp via Evolution. Retorna True/False. Reutilizável."""
    import urllib.request as _ur, urllib.error as _ue, json as _js
    conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s", (sid,), 'one')
    if not conn:
        return False
    evo_url = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
    evo_key = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
    eurl = (evo_url['valor'] if evo_url else conn['evolution_url']).rstrip('/')
    ekey = evo_key['valor'] if evo_key else conn['instance_key']
    inst = conn['instance_name']
    num = ''.join(ch for ch in (numero or '') if ch.isdigit())
    if not num:
        return False
    if not num.startswith('55') and len(num) <= 11:
        num = '55' + num
    for pl in [{'number': num, 'text': texto}, {'number': num, 'textMessage': {'text': texto}}]:
        try:
            req = _ur.Request(eurl + '/message/sendText/' + inst,
                              data=_js.dumps(pl).encode(),
                              headers={'Content-Type': 'application/json', 'apikey': ekey}, method='POST')
            with _ur.urlopen(req, timeout=15):
                return True
        except Exception:
            continue
    return False

def _disparo_processar_lote(sid, campanha_id, limite_lote=8):
    """Processa um LOTE pequeno de envios pendentes de uma campanha.
    Chamado pelo cron (ou pela rota de processar). Não usa sleep longo nem thread —
    confiável no Render. Envia poucos por vez; o cron chama de novo no próximo ciclo."""
    camp = db_exec("SELECT * FROM disparo_campanha WHERE id=%s AND salon_id=%s", (campanha_id, sid), 'one')
    if not camp or camp['status'] in ('cancelada', 'concluida'):
        return {'enviados': 0, 'status': camp['status'] if camp else 'inexistente'}

    # Quanto já foi enviado HOJE (para respeitar limite diário)
    hoje_ini = today_br().isoformat() + ' 00:00:00'
    enviados_hoje_row = db_exec("""SELECT COUNT(*) as n FROM disparo_log
        WHERE campanha_id=%s AND status='enviado' AND enviado_em >= %s""",
        (campanha_id, hoje_ini), 'one')
    enviados_hoje = enviados_hoje_row['n'] if enviados_hoje_row else 0
    if enviados_hoje >= camp['limite_dia']:
        db_exec("UPDATE disparo_campanha SET status='pausada_limite' WHERE id=%s", (campanha_id,))
        db_commit()
        return {'enviados': 0, 'status': 'pausada_limite'}

    # Pega os próximos pendentes (ainda não enviados)
    resto_dia = camp['limite_dia'] - enviados_hoje
    qtd = min(limite_lote, resto_dia)
    pendentes = db_exec("""SELECT id, cli_id, numero FROM disparo_log
        WHERE campanha_id=%s AND status='pendente' ORDER BY id LIMIT %s""",
        (campanha_id, qtd), 'all')
    if not pendentes:
        # Acabou: marca como concluída
        db_exec("UPDATE disparo_campanha SET status='concluida' WHERE id=%s", (campanha_id,))
        db_commit()
        return {'enviados': 0, 'status': 'concluida'}

    enviados = 0
    for p in pendentes:
        # Buscar nome do cliente para personalizar
        cli = db_exec("SELECT nome FROM clientes WHERE id=%s AND salon_id=%s", (p['cli_id'], sid), 'one')
        primeiro = ''
        if cli and cli['nome']:
            primeiro = cli['nome'].split(' ')[0]
        texto = camp['mensagem'].replace('{nome}', primeiro) if '{nome}' in camp['mensagem'] else camp['mensagem']
        ok = False
        try:
            ok = _enviar_wpp(sid, p['numero'], texto)
        except Exception:
            ok = False
        db_exec("UPDATE disparo_log SET status=%s, enviado_em=NOW() WHERE id=%s",
                ('enviado' if ok else 'falha', p['id']))
        if ok:
            db_exec("UPDATE disparo_campanha SET enviados=enviados+1 WHERE id=%s", (campanha_id,))
            enviados += 1
        else:
            db_exec("UPDATE disparo_campanha SET falhas=falhas+1 WHERE id=%s", (campanha_id,))
        db_commit()

    # Se não restam pendentes, conclui
    rest = db_exec("SELECT COUNT(*) as n FROM disparo_log WHERE campanha_id=%s AND status='pendente'", (campanha_id,), 'one')
    if rest and rest['n'] == 0:
        db_exec("UPDATE disparo_campanha SET status='concluida' WHERE id=%s", (campanha_id,))
        db_commit()
    return {'enviados': enviados, 'status': 'rodando'}

@app.route('/api/disparo/iniciar', methods=['POST'])
def disparo_iniciar():
    sid, err = require_salon()
    if err: return err
    d = request.json or {}
    mensagem = (d.get('mensagem') or '').strip()
    titulo = (d.get('titulo') or 'Aviso').strip()
    limite_dia = int(d.get('limite_dia') or 80)
    filtro = d.get('filtro', 'todos')
    if len(mensagem) < 5:
        return jsonify({'ok': False, 'erro': 'Escreva a mensagem que será enviada.'})
    if limite_dia > 200:
        limite_dia = 200

    hoje_iso = today_br()
    def dlimite(dias): return (hoje_iso - datetime.timedelta(days=dias)).isoformat()
    if filtro == 'ativos_30':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita >= %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(30)), 'all')
    elif filtro == 'ativos_60':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita >= %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(60)), 'all')
    elif filtro == 'ativos_90':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita >= %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(90)), 'all')
    elif filtro == 'ativos_180':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita >= %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(180)), 'all')
    elif filtro == 'inativos_90_180':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita < %s AND ultima_visita >= %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(90), dlimite(180)), 'all')
    elif filtro == 'inativos_180':
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              AND ultima_visita < %s ORDER BY ultima_visita DESC""",
                           (sid, dlimite(180)), 'all')
    else:
        clientes = db_exec("""SELECT id,nome,tel FROM clientes WHERE salon_id=%s AND tel!='' AND ativo=1
                              ORDER BY ultima_visita DESC NULLS LAST""", (sid,), 'all')

    clientes = clientes or []
    if not clientes:
        return jsonify({'ok': False, 'erro': 'Nenhum cliente com telefone encontrado para esse filtro.'})

    camp = db_exec("""INSERT INTO disparo_campanha (salon_id,titulo,mensagem,total,limite_dia,status)
                      VALUES (%s,%s,%s,%s,%s,'rodando') RETURNING id""",
                   (sid, titulo, mensagem, len(clientes), limite_dia), 'one')
    db_commit()
    campanha_id = camp['id']

    # Registrar TODOS os destinatários como 'pendente' (fila persistente no banco)
    for c in clientes:
        db_exec("""INSERT INTO disparo_log (salon_id,campanha_id,cli_id,numero,status)
                   VALUES (%s,%s,%s,%s,'pendente')
                   ON CONFLICT (campanha_id,cli_id) DO NOTHING""",
                (sid, campanha_id, c['id'], c['tel']))
    db_commit()

    # Processa o primeiro lote imediatamente (para o usuário ver saída na hora)
    res = _disparo_processar_lote(sid, campanha_id, limite_lote=5)

    return jsonify({'ok': True, 'campanha_id': campanha_id, 'total': len(clientes),
                    'primeiro_lote': res.get('enviados', 0),
                    'msg': 'Disparo iniciado! ' + str(res.get('enviados',0)) + ' enviados agora. O restante sai aos poucos (limite ' + str(limite_dia) + '/dia).'})

@app.route('/api/disparo/processar', methods=['GET','POST'])
def disparo_processar():
    """Processa lotes pendentes. Pode ser chamado pelo cron (com ?key=) ou pelo painel (logado)."""
    key = request.args.get('key', '')
    keyrow = db_exec("SELECT valor FROM sistema_global WHERE chave='cron_key'", fetch='one')
    cron_key = keyrow['valor'] if keyrow else 'musa_cron_2024'

    if key and key == cron_key:
        # Modo cron: processa um lote de CADA campanha ativa de todos os salões
        camps = db_exec("SELECT id, salon_id FROM disparo_campanha WHERE status='rodando'", fetch='all')
        total = 0
        for c in (camps or []):
            r = _disparo_processar_lote(c['salon_id'], c['id'], limite_lote=8)
            total += r.get('enviados', 0)
        return jsonify({'ok': True, 'enviados': total})

    # Modo painel: processa a campanha do salão logado
    sid, err = require_salon()
    if err: return err
    cid = request.args.get('campanha_id')
    if not cid:
        return jsonify({'ok': False, 'erro': 'campanha_id necessário'})
    r = _disparo_processar_lote(sid, int(cid), limite_lote=8)
    return jsonify({'ok': True, **r})

@app.route('/api/disparo/status/<int:campanha_id>', methods=['GET'])
def disparo_status(campanha_id):
    sid, err = require_salon()
    if err: return err
    camp = db_exec("SELECT * FROM disparo_campanha WHERE id=%s AND salon_id=%s", (campanha_id, sid), 'one')
    if not camp:
        return jsonify({'ok': False, 'erro': 'Campanha não encontrada'})
    return jsonify({'ok': True, 'campanha': dict(camp)})

@app.route('/api/disparo/lista/<int:campanha_id>', methods=['GET'])
def disparo_lista(campanha_id):
    """Lista detalhada dos envios da campanha (para acompanhamento ao vivo)."""
    sid, err = require_salon()
    if err: return err
    camp = db_exec("SELECT * FROM disparo_campanha WHERE id=%s AND salon_id=%s", (campanha_id, sid), 'one')
    if not camp:
        return jsonify({'ok': False, 'erro': 'Campanha não encontrada'})
    rows = db_exec("""SELECT dl.cli_id, dl.numero, dl.status, dl.enviado_em, c.nome as cli_nome
        FROM disparo_log dl LEFT JOIN clientes c ON c.id=dl.cli_id
        WHERE dl.campanha_id=%s ORDER BY
        CASE dl.status WHEN 'enviado' THEN 1 WHEN 'falha' THEN 2 ELSE 3 END, dl.enviado_em DESC NULLS LAST, dl.id
        LIMIT 500""", (campanha_id,), 'all')
    return jsonify({'ok': True, 'campanha': dict(camp), 'itens': [dict(r) for r in (rows or [])]})

@app.route('/api/disparo/cancelar/<int:campanha_id>', methods=['POST'])
def disparo_cancelar(campanha_id):
    sid, err = require_salon()
    if err: return err
    db_exec("UPDATE disparo_campanha SET status='cancelada' WHERE id=%s AND salon_id=%s", (campanha_id, sid))
    db_commit()
    return jsonify({'ok': True})

@app.route('/api/disparo/retomar/<int:campanha_id>', methods=['POST'])
def disparo_retomar(campanha_id):
    """Retoma uma campanha pausada por limite — volta status para rodando e processa um lote."""
    sid, err = require_salon()
    if err: return err
    camp = db_exec("SELECT * FROM disparo_campanha WHERE id=%s AND salon_id=%s", (campanha_id, sid), 'one')
    if not camp:
        return jsonify({'ok': False, 'erro': 'Campanha não encontrada'})
    rest = db_exec("SELECT COUNT(*) as n FROM disparo_log WHERE campanha_id=%s AND status='pendente'", (campanha_id,), 'one')
    if not rest or rest['n'] == 0:
        db_exec("UPDATE disparo_campanha SET status='concluida' WHERE id=%s", (campanha_id,))
        db_commit()
        return jsonify({'ok': True, 'msg': 'Todos já receberam. Campanha concluída.'})
    db_exec("UPDATE disparo_campanha SET status='rodando' WHERE id=%s", (campanha_id,))
    db_commit()
    res = _disparo_processar_lote(sid, campanha_id, limite_lote=5)
    return jsonify({'ok': True, 'msg': 'Retomado! ' + str(res.get('enviados',0)) + ' enviados agora; o restante segue aos poucos.'})


@app.route('/api/contato-auto/config', methods=['GET'])
def contato_auto_get():
    sid, err = require_salon()
    if err: return err
    rows = db_exec("SELECT * FROM contato_auto_config WHERE salon_id=%s", (sid,), 'all')
    out = {}
    for r in (rows or []):
        out[r['tipo']] = dict(r)
    return jsonify(out)

@app.route('/api/contato-auto/diagnostico', methods=['GET'])
def contato_auto_diag():
    """Mostra o estado REAL no banco de cada tipo de contato automático."""
    sid, err = require_salon()
    if err: return err
    rows = db_exec("SELECT tipo, ativo, modo, horario, dias_semana, ultimo_envio FROM contato_auto_config WHERE salon_id=%s", (sid,), 'all')
    return jsonify({'salon_id': sid, 'configs': [dict(r) for r in (rows or [])]})

@app.route('/api/contato-auto/desativar-tudo', methods=['POST'])
def contato_auto_desativar_tudo():
    """Desativa TODOS os contatos automáticos deste salão (botão de emergência)."""
    sid, err = require_salon()
    if err: return err
    db_exec("UPDATE contato_auto_config SET ativo=0 WHERE salon_id=%s", (sid,))
    db_commit()
    return jsonify({'ok': True, 'msg': 'Todos os envios automáticos foram desativados.'})

@app.route('/api/contato-auto/config', methods=['POST'])
def contato_auto_set():
    sid, err = require_salon()
    if err: return err
    d = request.json or {}
    tipo = d.get('tipo', '')
    if tipo not in ('retorno', 'inativo', 'lembrete'):
        return jsonify({'ok': False, 'erro': 'tipo inválido'})
    ex = db_exec("SELECT id FROM contato_auto_config WHERE salon_id=%s AND tipo=%s", (sid, tipo), 'one')
    if ex:
        db_exec("""UPDATE contato_auto_config SET ativo=%s,modo=%s,msg_fixa=%s,horario=%s,
                   dias_semana=%s,dias_inativo=%s,dias_antes=%s,
                   lemb_1dia=%s,lemb_2h=%s,lemb_1h=%s,confirma_palavra=%s,
                   atualizado_em=NOW() WHERE salon_id=%s AND tipo=%s""",
                (d.get('ativo',0), d.get('modo','fixo'), d.get('msg_fixa',''), d.get('horario','08:00'),
                 d.get('dias_semana','1,2,3,4,5'), d.get('dias_inativo',40), d.get('dias_antes',7),
                 str(d.get('lemb_1dia','1')), str(d.get('lemb_2h','0')), str(d.get('lemb_1h','0')),
                 str(d.get('confirma_palavra','1')), sid, tipo))
    else:
        db_exec("""INSERT INTO contato_auto_config (salon_id,tipo,ativo,modo,msg_fixa,horario,dias_semana,dias_inativo,dias_antes,lemb_1dia,lemb_2h,lemb_1h,confirma_palavra)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sid, tipo, d.get('ativo',0), d.get('modo','fixo'), d.get('msg_fixa',''),
                 d.get('horario','08:00'), d.get('dias_semana','1,2,3,4,5'), d.get('dias_inativo',40), d.get('dias_antes',7),
                 str(d.get('lemb_1dia','1')), str(d.get('lemb_2h','0')), str(d.get('lemb_1h','0')), str(d.get('confirma_palavra','1'))))
    db_commit()
    return jsonify({'ok': True})

def _gerar_msg_contato(sid, tipo, cli_nome, cfg_ia, cfg_auto, svc_nome=''):
    """Gera a mensagem: fixa ou via IA, conforme o modo configurado.
    svc_nome: serviço que a cliente fez (usado para personalizar o retorno)."""
    nome_salao = ''
    sal = db_exec("SELECT nome FROM saloes WHERE id=%s", (sid,), 'one')
    if sal: nome_salao = sal['nome']
    primeiro_nome = (cli_nome or '').split(' ')[0] if cli_nome else 'tudo bem'
    svc = (svc_nome or '').strip()
    if cfg_auto.get('modo') == 'ia' and cfg_ia and cfg_ia.get('groq_key'):
        try:
            from ia_wpp import groq_chat
            if tipo == 'retorno':
                if svc:
                    instrucao = ('Escreva uma mensagem curta e calorosa de WhatsApp para a cliente ' + primeiro_nome +
                                 ' lembrando que está chegando a hora do retorno do serviço "' + svc + '" que ela fez no salão ' + nome_salao + '. '
                                 'Mencione o serviço pelo nome e reforce com gentileza o benefício de manter o resultado em dia '
                                 '(ex: manter o visual bonito, o efeito do tratamento). Convide-a a reservar um horário. '
                                 'No máximo 3 linhas, 1 ou 2 emojis, sem aspas.')
                else:
                    instrucao = ('Escreva uma mensagem curta e calorosa de WhatsApp para lembrar a cliente ' + primeiro_nome +
                                 ' de que está na hora de voltar ao salão ' + nome_salao + ' para o retorno do tratamento. '
                                 'Seja gentil, no máximo 3 linhas, com 1 emoji.')
            else:
                instrucao = ('Escreva uma mensagem curta e carinhosa de WhatsApp sentindo saudade da cliente ' + primeiro_nome +
                             ', que não vem ao salão ' + nome_salao + ' há um tempo' +
                             (' (o último serviço dela foi "' + svc + '")' if svc else '') +
                             '. Convide-a a voltar, sem cobrança. Máximo 3 linhas, 1 emoji.')
            resp, erro = groq_chat([{'role':'system','content':'Você é a recepcionista do salão. Responda apenas com a mensagem, sem aspas.'},
                                    {'role':'user','content':instrucao}], max_tokens=200, groq_key=cfg_ia['groq_key'])
            if resp and not erro:
                return resp.strip()
        except Exception as ex:
            print('Erro IA contato auto:', ex)
    # Mensagem fixa (ou fallback)
    base = cfg_auto.get('msg_fixa') or ''
    if not base.strip():
        if tipo == 'retorno':
            if svc:
                base = 'Oi {nome}! 💕 Passando para lembrar que está chegando a hora do retorno do seu *' + svc + '* aqui no ' + nome_salao + '. Quer que eu já reserve um horário para você? ✨'
            else:
                base = 'Oi {nome}! 💕 Passando para lembrar que está chegando a hora do seu retorno aqui no ' + nome_salao + '. Quer que eu já reserve um horário para você?'
        else:
            base = 'Oi {nome}! 💕 Sentimos sua falta aqui no ' + nome_salao + '! Que tal agendar uma visita? Vai ser um prazer te receber de novo. ✨'
    return base.replace('{nome}', primeiro_nome).replace('{servico}', svc or 'seu serviço')

@app.route('/api/contato-auto/disparar', methods=['POST', 'GET'])
def contato_auto_disparar():
    """Dispara os contatos automáticos elegíveis. Chamado pelo cron externo OU pelo botão 'enviar agora'.
    Para cron: aceita ?key=CHAVE_GLOBAL e processa TODOS os salões no horário certo.
    Para botão: usa sessão do salão e ignora horário (envio manual)."""
    import datetime as _dt
    forcar = False
    sids = []
    # Modo cron (sem sessão): valida chave global
    key = request.args.get('key', '')
    keyrow = db_exec("SELECT valor FROM sistema_global WHERE chave='cron_key'", fetch='one')
    cron_key = keyrow['valor'] if keyrow else 'musa_cron_2024'
    if key and key == cron_key:
        rows = db_exec("SELECT DISTINCT salon_id FROM contato_auto_config WHERE ativo=1", fetch='all')
        sids = [r['salon_id'] for r in (rows or [])]
    else:
        sid, err = require_salon()
        if err: return err
        sids = [sid]
        forcar = (request.json or {}).get('forcar', True) if request.method=='POST' else True
        tipo_manual = (request.json or {}).get('tipo','') if request.method=='POST' else ''

    agora = _dt.datetime.utcnow() - _dt.timedelta(hours=3)  # horário de Brasília
    hoje = agora.date()
    dia_semana = str(agora.isoweekday())  # 1=seg ... 7=dom
    hora_atual = agora.strftime('%H:%M')

    total_enviado = 0
    detalhes = []
    for s in sids:
        cfgs = db_exec("SELECT * FROM contato_auto_config WHERE salon_id=%s AND ativo=1", (s,), 'all')
        cfg_ia = db_exec("SELECT * FROM wpp_ia_config WHERE salon_id=%s", (s,), 'one')
        for cfg in (cfgs or []):
            cfg = dict(cfg)
            tipo = cfg['tipo']
            # TRAVA DE SEGURANÇA: nunca envia se o tipo não estiver explicitamente ativado.
            # (Mesmo no envio manual: o botão "enviar agora" só vale se a função estiver ligada.)
            if str(cfg.get('ativo', 0)) not in ('1', 'True', 'true'):
                continue
            if not forcar:
                # Cron: respeitar dia da semana e horário (janela de 1h)
                if dia_semana not in (cfg.get('dias_semana') or '').split(','):
                    continue
                ch = cfg.get('horario','08:00')
                if not (ch <= hora_atual <= _add_min(ch, 59)):
                    continue
                if cfg.get('ultimo_envio') and str(cfg['ultimo_envio']) == str(hoje):
                    continue  # já enviou hoje
            else:
                if 'tipo_manual' in dir() and tipo_manual and tipo != tipo_manual:
                    continue
            # Buscar clientes elegíveis
            if tipo == 'retorno':
                dias_antes = int(cfg.get('dias_antes', 7) or 7)
                alvo = (hoje + _dt.timedelta(days=dias_antes)).isoformat()
                clientes = db_exec("""SELECT ra.id as alerta_id, ra.cli_id, ra.svc_nome, c.nome as cli_nome, c.tel as cli_tel
                                      FROM retorno_alertas ra JOIN clientes c ON c.id=ra.cli_id
                                      WHERE ra.salon_id=%s AND ra.realizado=0 AND ra.data_retorno<=%s""",
                                   (s, alvo), 'all')
            else:
                dias_in = cfg.get('dias_inativo', 40)
                limite = (hoje - _dt.timedelta(days=int(dias_in))).isoformat()
                clientes = db_exec("""SELECT c.id as cli_id, c.nome as cli_nome, c.tel as cli_tel,
                                         MAX(a.data) as ultima
                                      FROM clientes c LEFT JOIN agendamentos a ON a.cli_id=c.id AND a.salon_id=%s
                                      WHERE c.salon_id=%s AND c.ativo=1
                                      GROUP BY c.id, c.nome, c.tel
                                      HAVING MAX(a.data) IS NOT NULL AND MAX(a.data) <= %s""",
                                   (s, s, limite), 'all')
            enviados_tipo = 0
            diag = {'elegiveis': len(clientes or []), 'sem_telefone': 0, 'ja_contatado': 0, 'falha_envio': 0, 'enviados': 0}
            for cli in (clientes or []):
                cli = dict(cli)
                if not cli.get('cli_tel'):
                    diag['sem_telefone'] += 1
                    continue
                # Evitar reenvio: inativos = 45 dias; retorno/outros = 7 dias
                janela_dias = 45 if tipo == 'inativo' else 7
                jaenv = db_exec("""SELECT id FROM contato_auto_log WHERE salon_id=%s AND tipo=%s AND cli_id=%s
                                   AND enviado_em > NOW() - (%s || ' days')::interval""",
                                (s, tipo, cli['cli_id'], str(janela_dias)), 'one')
                if jaenv:
                    diag['ja_contatado'] += 1
                    continue
                msg = _gerar_msg_contato(s, tipo, cli.get('cli_nome',''), dict(cfg_ia) if cfg_ia else {}, cfg, cli.get('svc_nome',''))
                ok = _enviar_wpp(s, cli['cli_tel'], msg)
                if ok:
                    db_exec("INSERT INTO contato_auto_log (salon_id,tipo,cli_id,numero) VALUES (%s,%s,%s,%s)",
                            (s, tipo, cli['cli_id'], cli['cli_tel']))
                    enviados_tipo += 1
                    total_enviado += 1
                    diag['enviados'] += 1
                else:
                    diag['falha_envio'] += 1
            db_exec("UPDATE contato_auto_config SET ultimo_envio=%s WHERE salon_id=%s AND tipo=%s",
                    (hoje.isoformat(), s, tipo))
            db_commit()
            detalhes.append({'salon': s, 'tipo': tipo, 'enviados': enviados_tipo, 'diag': diag})

    # Resposta enxuta para chamadas via cron (evita 'output too large')
    if key and key == cron_key:
        return jsonify({'ok': True, 'total_enviado': total_enviado})
    return jsonify({'ok': True, 'total_enviado': total_enviado, 'detalhes': detalhes})

def _add_min(hhmm, mins):
    h, m = int(hhmm.split(':')[0]), int(hhmm.split(':')[1])
    tot = h*60 + m + mins
    return '%02d:%02d' % ((tot//60) % 24, tot % 60)

@app.route('/api/lembrete/disparar', methods=['POST', 'GET'])
def lembrete_disparar():
    """Dispara lembretes de agendamento na antecedência configurada. Chamado pelo cron OU botão."""
    import datetime as _dt
    key = request.args.get('key', '')
    keyrow = db_exec("SELECT valor FROM sistema_global WHERE chave='cron_key'", fetch='one')
    cron_key = keyrow['valor'] if keyrow else 'musa_cron_2024'
    forcar = False
    if key and key == cron_key:
        rows = db_exec("SELECT salon_id FROM contato_auto_config WHERE tipo='lembrete' AND ativo=1", fetch='all')
        sids = [r['salon_id'] for r in (rows or [])]
    else:
        sid, err = require_salon()
        if err: return err
        sids = [sid]
        forcar = True

    agora = _dt.datetime.utcnow() - _dt.timedelta(hours=3)  # Brasília
    total = 0
    for s in sids:
        cfg = db_exec("SELECT * FROM contato_auto_config WHERE salon_id=%s AND tipo='lembrete'", (s,), 'one')
        if not cfg:
            continue
        cfg = dict(cfg)
        if not forcar and not cfg.get('ativo'):
            continue
        cfg_ia = db_exec("SELECT * FROM wpp_ia_config WHERE salon_id=%s", (s,), 'one')
        palavra = (cfg.get('confirma_palavra') or '1').strip()
        # Antecedências ativas: (rótulo, minutos antes)
        janelas = []
        if str(cfg.get('lemb_1dia','1')) == '1': janelas.append(('1dia', 24*60))
        if str(cfg.get('lemb_2h','0'))  == '1': janelas.append(('2h', 120))
        if str(cfg.get('lemb_1h','0'))  == '1': janelas.append(('1h', 60))
        for rotulo, mins_antes in janelas:
            alvo = agora + _dt.timedelta(minutes=mins_antes)
            # Agendamentos no dia/hora alvo (janela de 60 min para o cron de hora em hora)
            data_alvo = alvo.date().isoformat()
            ags = db_exec("""SELECT a.id, a.cli_id, a.h_ini, a.data, c.nome as cli_nome, c.tel as cli_tel,
                                    s2.nome as svc_nome
                             FROM agendamentos a JOIN clientes c ON c.id=a.cli_id
                             LEFT JOIN servicos s2 ON s2.id=a.svc_id
                             WHERE a.salon_id=%s AND a.data=%s
                               AND a.status NOT IN ('cancelado','confirmado')""",
                          (s, data_alvo), 'all')
            for ag in (ags or []):
                ag = dict(ag)
                if not ag.get('cli_tel') or not ag.get('h_ini'):
                    continue
                # Hora do agendamento
                try:
                    hh, mm = int(ag['h_ini'].split(':')[0]), int(ag['h_ini'].split(':')[1])
                    dt_ag = _dt.datetime(alvo.year, alvo.month, alvo.day, hh, mm)
                except Exception:
                    continue
                # Está dentro da janela de envio? (entre mins_antes e mins_antes-60)
                delta_min = (dt_ag - agora).total_seconds() / 60.0
                if not (mins_antes - 60 < delta_min <= mins_antes):
                    continue
                # Já enviou esse lembrete para esse agendamento?
                ja = db_exec("SELECT id FROM lembrete_log WHERE ag_id=%s AND antecedencia=%s", (ag['id'], rotulo), 'one')
                if ja:
                    continue
                # Montar mensagem
                primeiro = (ag.get('cli_nome','') or '').split(' ')[0]
                quando = 'amanhã' if rotulo=='1dia' else ('em 2 horas' if rotulo=='2h' else 'em 1 hora')
                base = cfg.get('msg_fixa') or ''
                if base.strip():
                    msg = base.replace('{nome}', primeiro).replace('{hora}', ag['h_ini']).replace('{servico}', ag.get('svc_nome','seu horário') or 'seu horário').replace('{quando}', quando)
                else:
                    sal = db_exec("SELECT nome FROM saloes WHERE id=%s", (s,), 'one')
                    nome_salao = sal['nome'] if sal else 'nosso salão'
                    msg = ('Oi ' + primeiro + '! 💕 Passando para lembrar do seu horário ' + quando +
                           ' às ' + ag['h_ini'] + ' no ' + nome_salao + '.\n\nResponda *' + palavra +
                           '* para confirmar sua presença! 😊')
                if _enviar_wpp(s, ag['cli_tel'], msg):
                    db_exec("INSERT INTO lembrete_log (salon_id,ag_id,antecedencia) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (s, ag['id'], rotulo))
                    db_commit()
                    total += 1
    return jsonify({'ok': True, 'total_enviado': total})

# ─── WEBHOOK Evolution API ────────────────────────────────────────────────────
def _horarios_livres(sid, pro_id, data_iso, dur_min=60, abertura='09:00', fechamento='19:00'):
    """Retorna lista de horários livres (HH:MM) de um profissional num dia."""
    import datetime as _dt
    try:
        ags = db_exec("""SELECT h_ini, h_fim FROM agendamentos
                         WHERE salon_id=%s AND pro_id=%s AND data=%s
                         AND status NOT IN ('cancelado')""",
                      (sid, pro_id, data_iso), 'all')
    except Exception:
        ags = []
    ocupados = []
    for a in (ags or []):
        a = dict(a)
        try:
            ini = int(a['h_ini'].split(':')[0])*60 + int(a['h_ini'].split(':')[1])
            fim = int(a['h_fim'].split(':')[0])*60 + int(a['h_fim'].split(':')[1])
            ocupados.append((ini, fim))
        except Exception:
            continue
    ab = int(abertura.split(':')[0])*60 + int(abertura.split(':')[1])
    fc = int(fechamento.split(':')[0])*60 + int(fechamento.split(':')[1])
    # Se for hoje, não oferecer horários que já passaram (+30 min de margem)
    agora_br = _dt.datetime.utcnow() - _dt.timedelta(hours=3)
    if data_iso == agora_br.date().isoformat():
        min_agora = agora_br.hour*60 + agora_br.minute + 30
        if ab < min_agora:
            ab = ((min_agora + 29)//30)*30  # arredonda para próximo slot de 30
    livres = []
    t = ab
    while t + dur_min <= fc:
        conflito = any(not (t + dur_min <= o[0] or t >= o[1]) for o in ocupados)
        if not conflito:
            livres.append('%02d:%02d' % (t // 60, t % 60))
        t += 30  # slots de 30 min
    return livres


@app.route('/webhook/wpp/<int:sid>', methods=['POST'])
def wpp_webhook(sid):
    """Recebe mensagens da Evolution API e responde com IA."""
    data = request.json or {}
    print('=== WEBHOOK recebido salon', sid, '| event:', data.get('event'), '===')
    # Salvar último webhook para diagnóstico
    try:
        import json as _jdbg
        db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_webhook_'||%s, %s)
                   ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                (str(sid), _jdbg.dumps(data)[:2000]))
        db_commit()
    except Exception as _e:
        print('debug webhook save err:', _e)

    # Verificar se é mensagem recebida (aceita variações de formato do evento)
    evento = (data.get('event') or '').lower().replace('_', '.')
    if 'messages.upsert' not in evento:
        return jsonify({'ok': True})
    msg_data = data.get('data', {})
    if not msg_data:
        return jsonify({'ok': True})
    # data pode ser lista ou objeto
    if isinstance(msg_data, list):
        msg_data = msg_data[0] if msg_data else {}

    # Mensagem enviada pelo PRÓPRIO número (você respondeu manualmente pelo celular)
    key = msg_data.get('key', {})
    if key.get('fromMe'):
        # PAUSA AUTOMÁTICA: o humano assumiu esta conversa. A IA recua por 30 min.
        try:
            rjid = key.get('remoteJid', '')
            if '@s.whatsapp.net' in rjid:
                num_cli = ''.join(ch for ch in rjid if ch.isdigit())
                if num_cli:
                    db_exec("""INSERT INTO wpp_ia_pausa (salon_id,numero_cliente,pausado_ate,pausado_manual,atualizado_em)
                               VALUES (%s,%s, NOW() + INTERVAL '30 minutes', 0, NOW())
                               ON CONFLICT (salon_id,numero_cliente)
                               DO UPDATE SET pausado_ate=NOW() + INTERVAL '30 minutes', atualizado_em=NOW()""",
                            (sid, num_cli[-12:]))
                    db_commit()
        except Exception:
            pass
        return jsonify({'ok': True})

    remote_jid = key.get('remoteJid', '')
    # Ignorar grupos
    if '@g.us' in remote_jid:
        return jsonify({'ok': True})

    def _so_digitos(s):
        return ''.join(ch for ch in (s or '') if ch.isdigit())

    # O 'sender' do payload é o DONO da instância (salão), NÃO o cliente. Não usar!
    owner_inst = _so_digitos(msg_data.get('sender', '') or data.get('sender', ''))

    numero_cliente = ''
    # 1. Tentar campos que trazem o número real do cliente
    for campo in [key.get('remoteJidAlt',''), key.get('senderPn',''),
                  msg_data.get('remoteJidAlt',''), msg_data.get('senderPn',''),
                  key.get('participant',''), msg_data.get('participantPn','')]:
        if campo and '@s.whatsapp.net' in str(campo):
            numero_cliente = _so_digitos(campo)
            break

    # 2. Se remoteJid já é um número real (s.whatsapp.net), usar
    if not numero_cliente and '@s.whatsapp.net' in remote_jid:
        numero_cliente = _so_digitos(remote_jid)

    # 3. Se é @lid, tentar resolver via API de contatos da Evolution
    if not numero_cliente and '@lid' in remote_jid:
        lid_num = _so_digitos(remote_jid)
        try:
            import urllib.request as _ur2, json as _js2
            _eu2 = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
            _ek2 = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
            _url2 = (_eu2['valor'] if _eu2 else '').rstrip('/')
            _key2 = _ek2['valor'] if _ek2 else ''
            _inst2 = 'musa_' + str(sid)
            # Buscar contato pelo lid
            req2 = _ur2.Request(_url2 + '/chat/findContacts/' + _inst2,
                data=_js2.dumps({'where': {}}).encode(),
                headers={'Content-Type': 'application/json', 'apikey': _key2}, method='POST')
            with _ur2.urlopen(req2, timeout=10) as r2:
                contatos = _js2.loads(r2.read())
            if isinstance(contatos, list):
                for ct in contatos:
                    cid = str(ct.get('id','') or ct.get('remoteJid',''))
                    if lid_num in cid or cid.endswith('@lid'):
                        pn = ct.get('phoneNumber') or ct.get('number') or ''
                        if pn:
                            numero_cliente = _so_digitos(pn)
                            break
        except Exception as ex_lid:
            print('Erro resolvendo LID:', ex_lid)

    # 4. Último recurso: usar o lid mesmo (pode falhar no envio, mas registra)
    if not numero_cliente:
        numero_cliente = _so_digitos(remote_jid)

    numero_cliente = numero_cliente.split(':')[0]
    if not numero_cliente:
        return jsonify({'ok': True})
    # Se o número resolvido for igual ao dono da instância, é a própria mensagem — ignorar
    if owner_inst and numero_cliente == owner_inst:
        print('Ignorado: numero igual ao dono da instancia')
        return jsonify({'ok': True})
    # Chave normalizada da conversa: últimos 8 dígitos (estável mesmo se o número variar entre @lid e número real)
    conv_key = numero_cliente[-8:]
    # Log diagnóstico do número resolvido
    try:
        db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_num_'||%s, %s)
                   ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                (str(sid), 'remoteJid=' + remote_jid + ' | owner=' + owner_inst + ' | cliente=' + numero_cliente))
        db_commit()
    except: pass

    # Extrair texto da mensagem
    msg_obj = msg_data.get('message', {})
    texto = (
        msg_obj.get('conversation') or
        msg_obj.get('extendedTextMessage', {}).get('text') or
        msg_obj.get('imageMessage', {}).get('caption') or
        ''
    ).strip()

    if not texto:
        return jsonify({'ok': True})

    nome_cliente = msg_data.get('pushName', '') or ''

    # ── CONFIRMAÇÃO DE PRESENÇA (lembrete) — funciona mesmo com IA desativada ──
    try:
        cfg_lemb = db_exec("SELECT * FROM contato_auto_config WHERE salon_id=%s AND tipo='lembrete' AND ativo=1", (sid,), 'one')
        if cfg_lemb:
            palavra = (dict(cfg_lemb).get('confirma_palavra') or '1').strip().lower()
            texto_limpo = texto.strip().lower()
            if texto_limpo == palavra:
                import datetime as _dtc
                hoje_c = (_dtc.datetime.utcnow() - _dtc.timedelta(hours=3)).date().isoformat()
                # Buscar cliente por telefone
                cli_c = db_exec("""SELECT id FROM clientes WHERE salon_id=%s AND
                                   regexp_replace(tel,'[^0-9]','','g') LIKE %s""",
                                (sid, '%' + numero_cliente[-8:]), 'one')
                if cli_c:
                    # Agendamento futuro mais próximo, não confirmado
                    ag_c = db_exec("""SELECT id FROM agendamentos WHERE salon_id=%s AND cli_id=%s
                                      AND data>=%s AND status NOT IN ('cancelado','confirmado')
                                      ORDER BY data, h_ini LIMIT 1""",
                                   (sid, cli_c['id'], hoje_c), 'one')
                    if ag_c:
                        db_exec("UPDATE agendamentos SET status='confirmado' WHERE id=%s", (ag_c['id'],))
                        db_commit()
                        nome_salao_c = ''
                        sal_c = db_exec("SELECT nome FROM saloes WHERE id=%s", (sid,), 'one')
                        if sal_c: nome_salao_c = sal_c['nome']
                        _enviar_wpp(sid, numero_cliente,
                                    'Presença confirmada! ✅ Te esperamos no ' + (nome_salao_c or 'salão') + '. Até breve! 💕')
                        return jsonify({'ok': True, 'confirmado': True})
    except Exception as _ec:
        print('Erro confirmacao presenca:', _ec)

    # Buscar config IA do salão
    cfg = db_exec("SELECT * FROM wpp_ia_config WHERE salon_id=%s AND ativo=1", (sid,), 'one')
    if not cfg:
        return jsonify({'ok': True})

    # Verificar horário de atendimento
    import datetime as _dt
    tz_br = _dt.timezone(_dt.timedelta(hours=-3))
    agora = _dt.datetime.now(tz_br)
    hora_ini = cfg.get('horario_ini', '08:00')
    hora_fim = cfg.get('horario_fim', '20:00')
    dia_semana = str(agora.weekday() + 1)  # 1=Segunda ... 7=Domingo
    dias_ativos = (cfg.get('dias_semana') or '1,2,3,4,5,6').split(',')

    h_ini = int(hora_ini.split(':')[0]) * 60 + int(hora_ini.split(':')[1])
    h_fim = int(hora_fim.split(':')[0]) * 60 + int(hora_fim.split(':')[1])
    h_agora = agora.hour * 60 + agora.minute

    fora_horario = dia_semana not in dias_ativos or h_agora < h_ini or h_agora >= h_fim

    conn = db_exec("SELECT * FROM wpp_conexoes WHERE salon_id=%s AND ativo=1", (sid,), 'one')
    if not conn:
        return jsonify({'ok': True})

    # Buscar config global da Evolution (mais atualizada)
    _eu = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_url'", fetch='one')
    _ek = db_exec("SELECT valor FROM sistema_global WHERE chave='evolution_apikey'", fetch='one')
    _evolution_url = (_eu['valor'] if _eu else conn['evolution_url']).rstrip('/')
    _api_key       = _ek['valor'] if _ek else conn['instance_key']
    _instance      = conn['instance_name']

    def enviar_msg(numero, texto_resp):
        """Envia mensagem via Evolution API (v1.7.4 usa textMessage)."""
        import urllib.request as _ur, urllib.error as _ue, json as _js
        # Número só com dígitos
        num = ''.join(ch for ch in numero if ch.isdigit())
        # Garantir DDI 55 (Brasil) apenas se não tiver
        if not num.startswith('55') and len(num) <= 11:
            num = '55' + num
        # v1.7.4 usa textMessage; v2.x usa text. Tentar ambos.
        tentativas = [
            {'number': num, 'textMessage': {'text': texto_resp}},
            {'number': num, 'options': {'delay': 100, 'presence': 'composing'}, 'textMessage': {'text': texto_resp}},
            {'number': num, 'text': texto_resp},
        ]
        log = []
        for pl in tentativas:
            try:
                payload = _js.dumps(pl).encode()
                req = _ur.Request(
                    _evolution_url + '/message/sendText/' + _instance,
                    data=payload,
                    headers={'Content-Type': 'application/json', 'apikey': _api_key},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode('utf-8', errors='ignore')
                    log.append('OK ' + str(resp.status) + ' payload=' + _js.dumps(pl)[:60] + ' resp=' + body[:300])
                    _salvar_envio_log(sid, 'SUCESSO', num, ' || '.join(log))
                    return True
            except _ue.HTTPError as he:
                eb = he.read().decode('utf-8', errors='ignore')
                log.append('HTTP ' + str(he.code) + ': ' + eb[:120])
            except Exception as ex:
                log.append('ERR: ' + str(ex)[:120])
        _salvar_envio_log(sid, 'FALHA', num, ' | '.join(log))
        return False

    def _salvar_envio_log(salon, status, num, detalhe):
        try:
            db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_send_'||%s, %s)
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (str(salon), status + ' | num=' + num + ' | ' + detalhe))
            db_commit()
        except Exception as _e:
            print('log envio err:', _e)

    def simular_digitando(numero, texto_resp):
        """Mostra 'digitando...' e pausa proporcional ao tamanho da resposta (humaniza)."""
        import urllib.request as _ur, json as _js, time as _t
        num = ''.join(ch for ch in numero if ch.isdigit())
        if not num.startswith('55') and len(num) <= 11:
            num = '55' + num
        # Tempo de digitação: ~50ms por caractere, entre 1.5s e 6s
        n_chars = len(texto_resp or '')
        segundos = min(6.0, max(1.5, n_chars * 0.05))
        try:
            pl = _js.dumps({'number': num, 'presence': 'composing', 'delay': int(segundos * 1000)}).encode()
            req = _ur.Request(_evolution_url + '/chat/sendPresence/' + _instance,
                              data=pl, headers={'Content-Type': 'application/json', 'apikey': _api_key}, method='POST')
            _ur.urlopen(req, timeout=10)
        except Exception:
            pass
        _t.sleep(segundos)

    # Salvar mensagem do cliente
    db_exec("INSERT INTO wpp_conversas (salon_id,numero_cliente,nome_cliente,role,content) VALUES (%s,%s,%s,'user',%s)",
            (sid, conv_key, nome_cliente, texto))
    db_commit()

    modo = cfg.get('modo_atendimento', '24h') or '24h'

    if modo == '24h':
        # IA responde sempre, a qualquer hora
        responder_agora = True
    elif modo == 'fora_expediente':
        # IA só responde FORA do horário comercial (no expediente, atende humano)
        responder_agora = fora_horario
    else:
        # desativado (não deveria chegar aqui, pois ativo=0, mas por segurança)
        responder_agora = False

    if not responder_agora:
        # No modo 'fora_expediente' dentro do expediente: a IA fica em silêncio
        # (quem atende é a equipe). Não envia nada.
        return jsonify({'ok': True})

    # HANDOFF: verificar se esta conversa está pausada (humano assumiu)
    try:
        pausa = db_exec("""SELECT pausado_ate, pausado_manual FROM wpp_ia_pausa
                           WHERE salon_id=%s AND numero_cliente=%s""",
                        (sid, numero_cliente[-12:]), 'one')
        if pausa:
            # Pausa manual (botão "assumir") fica até você devolver
            if pausa.get('pausado_manual') == 1:
                return jsonify({'ok': True})
            # Pausa automática: ativa enquanto pausado_ate ainda está no futuro
            if pausa.get('pausado_ate'):
                ainda = db_exec("SELECT (%s > NOW()) as ativa", (pausa['pausado_ate'],), 'one')
                if ainda and ainda.get('ativa'):
                    return jsonify({'ok': True})
    except Exception:
        pass

    # Buscar contexto
    salao  = db_exec("SELECT * FROM saloes WHERE id=%s", (sid,), 'one')
    pros   = db_exec("SELECT id,nome,cargo,ativo FROM profissionais WHERE salon_id=%s AND ativo=1", (sid,), 'all')
    svcs   = db_exec("SELECT id,nome,preco,duracao_min,ativo FROM servicos WHERE salon_id=%s AND ativo=1", (sid,), 'all')
    hist_c = db_exec("""SELECT role,content FROM wpp_conversas
        WHERE salon_id=%s AND numero_cliente=%s ORDER BY id DESC LIMIT 11""",
        (sid, conv_key), 'all')
    conversa = [{'role': r['role'], 'content': r['content']} for r in reversed(hist_c)]
    # Remover a última mensagem se for a do próprio cliente (já será enviada à parte)
    if conversa and conversa[-1]['role'] == 'user' and conversa[-1]['content'] == texto:
        conversa = conversa[:-1]
    # Diagnóstico: registrar histórico carregado (com prévia do conteúdo)
    try:
        previa = ' || '.join([(m['role'][:1] + ':' + m['content'][:25]) for m in conversa[-6:]])
        db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_hist_'||%s, %s)
                   ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                (str(sid), 'conv_key=' + conv_key + ' | hist_msgs=' + str(len(conversa)) + ' | ' + previa))
        db_commit()
    except Exception:
        pass

    # Buscar cliente pelos últimos 8 dígitos (ignora formatação: parênteses, traços, espaços)
    cli = db_exec("""SELECT id, nome FROM clientes WHERE salon_id=%s AND tel!='' AND
                     regexp_replace(tel,'[^0-9]','','g') LIKE %s
                     ORDER BY id LIMIT 1""",
                  (sid, '%' + numero_cliente[-8:]), 'one')
    hist_ags = []
    if cli:
        hist_ags = db_exec("""SELECT a.data,s.nome as svc_nome,p.nome as pro_nome FROM agendamentos a
            LEFT JOIN servicos s ON s.id=a.svc_id LEFT JOIN profissionais p ON p.id=a.pro_id
            WHERE a.salon_id=%s AND a.cli_id=%s ORDER BY a.data DESC LIMIT 5""",
            (sid, cli['id']), 'all')
        hist_ags = [dict(r) for r in hist_ags]

    try:
        from ia_wpp import responder as _resp, groq_chat as _gc
        # Calcular horários livres dos próximos 7 dias por profissional (para a IA não oferecer ocupados)
        try:
            import datetime as _dtl
            hoje_l = today_br()
            linhas_disp = []
            for p in pros:
                pd = dict(p)
                dur_padrao = 60
                dias_txt = []
                for d in range(0, 7):
                    dia_iso = (hoje_l + _dtl.timedelta(days=d)).isoformat()
                    livres = _horarios_livres(sid, pd['id'], dia_iso, dur_padrao,
                                              pd.get('h_inicio') or '09:00', pd.get('h_fim') or '19:00')
                    if livres:
                        # Mostra no máximo 8 horários por dia para não inchar o prompt
                        dias_txt.append('  ' + dia_iso + ': ' + ', '.join(livres[:8]))
                if dias_txt:
                    linhas_disp.append('• ' + pd['nome'] + ' [id=' + str(pd['id']) + ']:\n' + '\n'.join(dias_txt[:4]))
            disponibilidade_txt = '\n'.join(linhas_disp)
        except Exception as _ed:
            disponibilidade_txt = ''

        salao_dict = dict(salao) if salao else {}
        if disponibilidade_txt:
            salao_dict['_disponibilidade'] = disponibilidade_txt
        # Informa à IA se a cliente já tem cadastro (para pedir o nome se for nova)
        if cli and (cli.get('nome') or '').strip():
            salao_dict['_cliente_nome'] = cli['nome']
            salao_dict['_cliente_novo'] = False
        else:
            salao_dict['_cliente_novo'] = True

        resposta, comandos, erro = _resp(
            texto,
            salao_dict,
            [dict(p) for p in pros],
            [dict(s) for s in svcs],
            hist_ags,
            conversa,
            cfg.get('groq_key', ''),
            cfg.get('personalidade', '')
        )
        erro_ia = bool(erro or not resposta)
        if erro_ia:
            # Registrar o erro real para diagnóstico (mas não definir resposta ainda —
            # pode ser que a IA tenha mandado só o comando de agendar, sem texto)
            try:
                db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_iaerr_'||%s, %s)
                           ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                        (str(sid), 'erro=' + str(erro)[:400] + ' | resp_vazia=' + str(not resposta)))
                db_commit()
            except Exception:
                pass

        # Processar comandos da IA
        fez_agendamento = False
        dados_agendamento = None
        for cmd in (comandos or []):
            # ── VERIFICAR AGENDA: calcula horários livres e pede à IA para oferecer ──
            if cmd['tipo'] == 'VERIFICAR_AGENDA':
                p = cmd['params']
                dia = p.get('dia', '')
                pro_id_v = p.get('pro_id', '')
                # Se não veio profissional, usar o primeiro ativo
                if not pro_id_v and pros:
                    pro_id_v = dict(pros[0])['id']
                dur_v = 60
                if p.get('svc_id'):
                    sv = db_exec("SELECT duracao_min FROM servicos WHERE id=%s AND salon_id=%s", (p.get('svc_id'), sid), 'one')
                    if sv and sv.get('duracao_min'): dur_v = sv['duracao_min']
                livres = _horarios_livres(sid, pro_id_v, dia, dur_v) if dia else []
                # Separar manhã e tarde
                manha = [x for x in livres if int(x.split(':')[0]) < 12]
                tarde = [x for x in livres if int(x.split(':')[0]) >= 12]
                # Pedir à IA para formular a oferta de forma calorosa
                contexto_disp = 'HORÁRIOS LIVRES no dia ' + dia + ':\n'
                contexto_disp += 'Manhã: ' + (', '.join(manha[:6]) if manha else 'nenhum') + '\n'
                contexto_disp += 'Tarde: ' + (', '.join(tarde[:6]) if tarde else 'nenhum') + '\n'
                contexto_disp += ('\nInstrução: Pergunte primeiro se a cliente prefere MANHÃ ou TARDE (só os períodos que têm vaga). '
                                  'Depois que ela escolher o período, ofereça no máximo DUAS opções de horário daquele período para ela escolher. '
                                  'Seja calorosa e natural. Não liste todos os horários de uma vez. Não use comandos internos agora.')
                try:
                    r2, e2 = _gc([
                        {'role':'system','content':'Você é a recepcionista do salão ' + (dict(salao).get('nome','') if salao else '') + '. Seja calorosa e breve.'},
                        {'role':'user','content':contexto_disp}
                    ], max_tokens=250, groq_key=cfg.get('groq_key',''))
                    if r2 and not e2:
                        resposta = r2.strip()
                except Exception as e_v:
                    print('Erro VERIFICAR_AGENDA:', e_v)
                    if not manha and not tarde:
                        resposta = 'Poxa, não temos horários livres nesse dia 😕 Quer tentar outro dia?'

            # ── AGENDAR: grava com validação e anti-duplicação ──
            elif cmd['tipo'] == 'AGENDAR' and not fez_agendamento:
                p = cmd['params']
                try:
                    svc = db_exec("SELECT * FROM servicos WHERE id=%s AND salon_id=%s", (p.get('svc_id'), sid), 'one')
                    dur = (svc['duracao_min'] if svc and svc.get('duracao_min') else 60)
                    h_ini_ag = p.get('hora', '')
                    data_ag = p.get('data', '')
                    pro_ag = p.get('pro_id') or (dict(pros[0])['id'] if pros else None)
                    svc_ag = p.get('svc_id') or (dict(svcs[0])['id'] if svcs else None)
                    if not (h_ini_ag and data_ag and pro_ag and svc_ag):
                        print('AGENDAR incompleto:', p)
                        continue
                    tot_fim = int(h_ini_ag.split(':')[0])*60 + int(h_ini_ag.split(':')[1]) + dur
                    h_fim_ag = '%02d:%02d' % ((tot_fim//60)%24, tot_fim%60)
                    cli_id = cli['id'] if cli else None
                    if not cli_id:
                        nome_novo = (p.get('cli_nome') or '').strip() or (nome_cliente or '').strip() or 'Cliente WhatsApp'
                        cur = db_exec("INSERT INTO clientes (salon_id,nome,tel,ativo,criado_em) VALUES (%s,%s,%s,1,%s) RETURNING id",
                                     (sid, nome_novo, numero_cliente, today_br().isoformat()), 'one')
                        cli_id = cur['id'] if cur else None
                    # Anti-duplicação: já existe agendamento igual?
                    dup = db_exec("""SELECT id FROM agendamentos WHERE salon_id=%s AND cli_id=%s AND data=%s AND h_ini=%s
                                     AND status NOT IN ('cancelado')""", (sid, cli_id, data_ag, h_ini_ag), 'one')
                    if dup:
                        fez_agendamento = True  # já existe, não duplica
                        continue
                    # Verificar se o horário ainda está livre
                    livres_check = _horarios_livres(sid, pro_ag, data_ag, dur)
                    if h_ini_ag not in livres_check:
                        resposta = 'Ah, esse horário acabou de ser preenchido 😕 Quer que eu veja outro horário para você?'
                        continue
                    if cli_id:
                        db_exec("INSERT INTO agendamentos (salon_id,cli_id,pro_id,svc_id,data,h_ini,h_fim,status) VALUES (%s,%s,%s,%s,%s,%s,%s,'agendado')",
                               (sid, cli_id, pro_ag, svc_ag, data_ag, h_ini_ag, h_fim_ag))
                        db_commit()
                        fez_agendamento = True
                        # Guardar dados para montar a confirmação ao cliente
                        try:
                            svc_nm = svc['nome'] if svc else 'seu serviço'
                            pro_r = db_exec("SELECT nome FROM profissionais WHERE id=%s AND salon_id=%s", (pro_ag, sid), 'one')
                            pro_nm = pro_r['nome'] if pro_r else ''
                            dados_agendamento = {'svc': svc_nm, 'pro': pro_nm, 'data': data_ag, 'hora': h_ini_ag}
                        except Exception:
                            dados_agendamento = {'svc': '', 'pro': '', 'data': data_ag, 'hora': h_ini_ag}
                except Exception as ex_ag:
                    print('Erro ao agendar via IA:', ex_ag)

            elif cmd['tipo'] == 'CANCELAR':
                p = cmd['params']
                try:
                    if p.get('ag_id'):
                        db_exec("UPDATE agendamentos SET status='cancelado' WHERE id=%s AND salon_id=%s", (p.get('ag_id'), sid))
                        db_commit()
                except Exception as ex_c:
                    print('Erro cancelar via IA:', ex_c)

    except Exception as ex:
        print('Erro geral webhook IA:', ex)
        try:
            import traceback as _tb
            db_exec("""INSERT INTO sistema_global (chave, valor) VALUES ('last_iaerr_'||%s, %s)
                       ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (str(sid), 'EXCECAO=' + str(ex)[:300] + ' | ' + _tb.format_exc()[-300:]))
            db_commit()
        except Exception:
            pass
        resposta = 'Tive um probleminha aqui! Por favor, ligue para nós. 🙏'

    # Montar resposta final: priorizar confirmação de agendamento
    try:
        if 'dados_agendamento' in dir() and dados_agendamento:
            da = dados_agendamento
            # Formatar data DD/MM
            try:
                _p = da['data'].split('-'); data_fmt = _p[2] + '/' + _p[1]
            except Exception:
                data_fmt = da['data']
            nome_sal = (dict(salao).get('nome') if salao else '') or 'salão'
            confirma = ('Prontinho! ✅ Seu agendamento está confirmado:\n\n'
                        + '✂️ ' + (da.get('svc') or 'Serviço')
                        + ('\n👩 ' + da['pro'] if da.get('pro') else '')
                        + '\n📅 ' + data_fmt + ' às ' + da.get('hora', '')
                        + '\n\nTe esperamos! 💕\n— ' + nome_sal)
            # Se a IA já tinha um texto de confirmação próprio, usa o dela; senão usa o padrão
            if not resposta or len(resposta.strip()) < 15 or ('erro_ia' in dir() and erro_ia):
                resposta = confirma
        elif ('erro_ia' in dir() and erro_ia) and (not resposta or len(resposta.strip()) < 2):
            resposta = 'Desculpe, tive um probleminha técnico. Pode repetir, por favor? 🙏'
    except Exception:
        if not resposta:
            resposta = 'Desculpe, tive um probleminha técnico. Pode repetir, por favor? 🙏'

    # Garantia final: nunca enviar vazio
    if not resposta or not resposta.strip():
        resposta = 'Desculpe, pode repetir por favor? 😊'

    # Enviar resposta (com digitação humanizada antes)
    simular_digitando(numero_cliente + '@s.whatsapp.net', resposta)
    enviar_msg(numero_cliente + '@s.whatsapp.net', resposta)
    db_exec("INSERT INTO wpp_conversas (salon_id,numero_cliente,nome_cliente,role,content) VALUES (%s,%s,%s,'assistant',%s)",
            (sid, conv_key, nome_cliente, resposta))
    db_commit()
    return jsonify({'ok': True})

# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'app': 'Musa SaaS', 'version': '1.0'})

# ─── INICIALIZAÇÃO ────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as _e:
    print("Aviso init_db:", _e)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("  Musa SaaS v1.0")
    print(f"  http://localhost:{port}")
    print("=" * 50)
    app.run(debug=False, host='0.0.0.0', port=port)
