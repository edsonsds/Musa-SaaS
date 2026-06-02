#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Musa SaaS — Gestão para Salões (Multi-Tenant)
Arquitetura: Flask + PostgreSQL (Neon)
Cada salão tem salon_id isolado em todas as tabelas operacionais.
Super-admin gerencia cadastro de salões via /admin
"""

from flask import Flask, jsonify, request, send_from_directory, session, g, redirect, url_for
import os, json, datetime, re, hashlib, psycopg2, psycopg2.extras

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('MUSA_SECRET_KEY', 'musa_saas_sk_2024_change_me')

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── BANCO ────────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        conn = psycopg2.connect(DATABASE_URL)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pacotes (
        id SERIAL PRIMARY KEY,
        salon_id INTEGER NOT NULL,
        nome TEXT,
        descricao TEXT DEFAULT '',
        preco REAL DEFAULT 0,
        validade_dias INTEGER DEFAULT 30
    )""")

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Banco inicializado com sucesso.")

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

    db_commit()

# ─── PERMISSÕES ───────────────────────────────────────────────────────────────
ALL_TELAS = ['dashboard','agenda','clientes','profissionais','servicos','caixa',
             'comandas','comissoes','faturamento','despesas','agfin','retornos',
             'inativos','estoque','varejo','ocorrencias','avaliacoes','importar',
             'config','aniversariantes','metas','filaespera','exportar','lixeira','comvarejo']
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

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

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
        return jsonify({'ok': False, 'erro': 'Salão não identificado. Informe o código do salão.'})

    salon_id = int(salon_id)

    # Verificar se salão existe e está ativo
    salao = db_exec("SELECT * FROM saloes WHERE id=%s AND ativo=1", (salon_id,), 'one')
    if not salao:
        return jsonify({'ok': False, 'erro': 'Salão não encontrado ou inativo'})

    # 1) Login como usuário (admin/recepcionista)
    u = db_exec("SELECT * FROM usuarios WHERE salon_id=%s AND login=%s AND ativo=1",
                (salon_id, login_val), 'one')
    if u and u['senha_hash'] == hash_senha(senha):
        session['salon_id'] = salon_id
        session['salon_nome'] = salao['nome']
        session['uid']     = u['id']
        session['unome']   = u['nome']
        session['uperfil'] = u['perfil']
        session.pop('pro_id', None)
        return jsonify({'ok': True, 'nome': u['nome'], 'perfil': u['perfil'],
                        'permissoes': get_permissoes(dict(u)), 'salon_nome': salao['nome']})

    # 2) Login como profissional (por email)
    email = login_val.lower()
    p = db_exec("SELECT * FROM profissionais WHERE salon_id=%s AND LOWER(email)=%s AND ativo=1 AND pode_login=1",
                (salon_id, email), 'one')
    if p and p['senha_hash'] and p['senha_hash'] == hash_senha(senha):
        session['salon_id']   = salon_id
        session['salon_nome'] = salao['nome']
        session['uid']        = -p['id']
        session['unome']      = p['nome']
        session['uperfil']    = 'profissional'
        session['pro_id']     = p['id']
        session['pro_ver_comissao'] = bool(p['pode_ver_comissao'])
        return jsonify({'ok': True, 'nome': p['nome'], 'perfil': 'profissional',
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
                        'permissoes': ['agenda_propria'] + (['comissao_propria'] if p['pode_ver_comissao'] else [])})
    u = db_exec("SELECT * FROM usuarios WHERE id=%s AND salon_id=%s AND ativo=1",
                (session['uid'], sid), 'one')
    if not u:
        session.clear()
        return jsonify({'logado': False})
    return jsonify({'logado': True, 'nome': u['nome'], 'perfil': u['perfil'],
                    'salon_nome': session.get('salon_nome',''),
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
            p.nome as pro_nome,p.cor as pro_cor FROM agendamentos a
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
    fat_hoje = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data=%s AND status='concluido'", (sid,hoje), 'one')['t']
    fat_sem  = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND status='concluido'", (sid,ini_sem), 'one')['t']
    fat_mes  = db_exec("SELECT COALESCE(SUM(preco),0) as t FROM agendamentos WHERE salon_id=%s AND data>=%s AND status='concluido'", (sid,ini_mes), 'one')['t']
    ags_hoje = db_exec("""SELECT a.*,c.nome as cli_nome,s.nome as svc_nome,p.nome as pro_nome,p.cor as pro_cor
        FROM agendamentos a LEFT JOIN clientes c ON c.id=a.cli_id LEFT JOIN servicos s ON s.id=a.svc_id LEFT JOIN profissionais p ON p.id=a.pro_id
        WHERE a.salon_id=%s AND a.data=%s ORDER BY a.h_ini""", (sid,hoje), 'all')
    estoque_baixo = db_exec("SELECT * FROM estoque WHERE salon_id=%s AND quantidade<=minimo", (sid,), 'all')
    grafico = []
    for i in range(29,-1,-1):
        d  = (today_br()-datetime.timedelta(days=i)).isoformat()
        v  = db_exec("SELECT COALESCE(SUM(valor),0) as t FROM caixa WHERE salon_id=%s AND data=%s AND tipo='entrada'", (sid,d), 'one')['t']
        grafico.append({'data':d,'valor':round(v,2)})
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
    q = """SELECT c.*,a.data,a.h_ini,a.pag,s.nome as svc_nome,cl.nome as cli_nome,p.nome as pro_nome
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
    rows = db_exec("""SELECT ra.*,c.nome as cli_nome,c.tel as cli_tel FROM retorno_alertas ra
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

# ─── CLIENTES INATIVOS ────────────────────────────────────────────────────────
@app.route('/api/clientes/inativos', methods=['GET'])
def clientes_inativos():
    sid, err = require_salon()
    if err: return err
    dias = int(request.args.get('dias', 40))
    corte = (today_br()-datetime.timedelta(days=dias)).isoformat()
    rows = db_exec("""SELECT c.*,
        (SELECT COUNT(*) FROM agendamentos WHERE salon_id=%s AND cli_id=c.id AND status='concluido') as total_ags,
        (SELECT COALESCE(SUM(preco),0) FROM agendamentos WHERE salon_id=%s AND cli_id=c.id AND status='concluido') as total_gasto
        FROM clientes c WHERE c.salon_id=%s AND c.ativo=1 AND (c.ultima_visita IS NULL OR c.ultima_visita='' OR c.ultima_visita<=%s)
        ORDER BY c.ultima_visita ASC NULLS FIRST""", (sid,sid,sid,corte), 'all')
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
        rows = db_exec("""SELECT av.*,c.nome as cli_nome,p.nome as pro_nome FROM avaliacoes av
            LEFT JOIN clientes c ON c.id=av.cli_id LEFT JOIN profissionais p ON p.id=av.pro_id
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
