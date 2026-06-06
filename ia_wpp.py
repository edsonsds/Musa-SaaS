#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Musa SaaS — Motor de IA para WhatsApp
Usa Groq (gratuito) com Llama 3 para responder clientes
"""

import os, json, re, datetime
import urllib.request, urllib.error

GROQ_URL     = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL   = 'llama-3.1-8b-instant'  # 500k tokens/dia grátis (5x mais que o 70b), rápido

def _get_groq_key():
    return os.environ.get('GROQ_API_KEY', '')

def _hoje_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).date()

def _hora_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).strftime('%H:%M')

def groq_chat(messages, max_tokens=600, groq_key=None):
    """Chama a API do Groq usando o SDK oficial (evita bloqueio Cloudflare 1010).
    Se o modelo principal bater rate limit, tenta modelos alternativos automaticamente."""
    if not groq_key:
        groq_key = _get_groq_key()
    if not groq_key:
        return None, "GROQ_API_KEY não configurada"

    # Lista de modelos por ordem de preferência (todos free tier)
    modelos = [GROQ_MODEL, 'llama-3.1-8b-instant', 'gemma2-9b-it']
    # Remover duplicados mantendo ordem
    vistos = set(); modelos = [m for m in modelos if not (m in vistos or vistos.add(m))]

    ultimo_erro = None
    for modelo in modelos:
        # Método 1: SDK oficial do Groq
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            completion = client.chat.completions.create(
                model=modelo,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return completion.choices[0].message.content.strip(), None
        except ImportError:
            break  # SDK não instalado, usar requests abaixo
        except Exception as ex:
            ultimo_erro = str(ex)
            # Se for rate limit (429), tenta o próximo modelo; senão, para
            if '429' in str(ex) or 'rate_limit' in str(ex).lower():
                continue
            return None, str(ex)

    # Método 2: fallback via requests (tenta os mesmos modelos)
    for modelo in modelos:
        try:
            import requests
            resp = requests.post(
                GROQ_URL,
                json={'model': modelo, 'messages': messages,
                      'max_tokens': max_tokens, 'temperature': 0.3},
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + groq_key},
                timeout=20
            )
            if resp.status_code == 200:
                data = resp.json()
                return data['choices'][0]['message']['content'].strip(), None
            ultimo_erro = 'HTTP ' + str(resp.status_code) + ': ' + resp.text[:200]
            if resp.status_code == 429:
                continue
            return None, ultimo_erro
        except Exception as ex:
            ultimo_erro = str(ex)
            continue

    return None, ultimo_erro or 'Falha ao gerar resposta'

def montar_sistema(salao, profissionais, servicos, historico_cliente=None, personalidade='', em_andamento=False):
    """Monta o system prompt com contexto completo do salão."""
    hoje    = _hoje_br()
    hora    = _hora_br()
    dia_sem = ['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo'][hoje.weekday()]

    # Horários livres (calculados pelo backend) — para não oferecer horário ocupado
    disp = (salao or {}).get('_disponibilidade', '')
    if disp:
        bloco_disp = "\nHORÁRIOS LIVRES (ofereça SOMENTE estes; nunca ofereça horário fora desta lista):\n" + disp + "\n"
    else:
        bloco_disp = ''

    svcs_txt = ''
    for s in servicos:
        if s.get('ativo', 1):
            preco = f"R$ {float(s.get('preco',0)):.2f}".replace('.',',')
            dur   = s.get('duracao_min', 60)
            svcs_txt += f"  • [id={s['id']}] {s['nome']} — {preco} ({dur} min)\n"

    pros_txt = ''
    for p in profissionais:
        if p.get('ativo', 1):
            cargo = p.get('cargo', '') or 'Profissional'
            pros_txt += f"  • [id={p['id']}] {p['nome']} ({cargo})\n"

    hist_txt = ''
    if historico_cliente:
        hist_txt = '\nHISTÓRICO DO CLIENTE:\n'
        for h in historico_cliente[:5]:
            hist_txt += f"  • {h.get('data','')} — {h.get('svc_nome','?')} com {h.get('pro_nome','?')}\n"

    nome_salao = salao.get('nome', 'Salão')
    tel_salao  = salao.get('telefone', '')
    end_salao  = salao.get('endereco', '')

    # Datas de referência para a IA usar nos comandos (evita erro de data)
    import datetime as _dtr
    _dias_pt = ['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo']
    ref_datas = 'DATAS DE REFERÊNCIA (use exatamente estas no formato YYYY-MM-DD):\n'
    for i in range(0, 8):
        d = hoje + _dtr.timedelta(days=i)
        rotulo = 'HOJE' if i == 0 else ('AMANHÃ' if i == 1 else _dias_pt[d.weekday()])
        ref_datas += '  • ' + rotulo + ' = ' + d.strftime('%Y-%m-%d') + ' (' + _dias_pt[d.weekday()] + ')\n'

    # Estado da conversa
    if em_andamento:
        bloco_estado = "\nA conversa JÁ está em andamento. NÃO cumprimente de novo nem repita perguntas já respondidas — apenas continue de onde parou.\n"
    else:
        bloco_estado = "\nPrimeira mensagem: cumprimente com calor, diga o nome do salão e pergunte como pode ajudar.\n"

    # Personalidade customizada pelo dono do salão (tem prioridade)
    bloco_personalidade = ''
    if personalidade and personalidade.strip():
        bloco_personalidade = f"""
PERSONALIDADE E INSTRUÇÕES DO SALÃO (definidas pelo dono — siga com prioridade):
{personalidade.strip()}
"""

    system = f"""Você é a recepcionista virtual do {nome_salao}, atendendo pelo WhatsApp. Seja calorosa, natural e breve, como uma pessoa real da equipe.
Hoje é {dia_sem}, {hoje.strftime('%d/%m/%Y')}, agora são {hora} (Brasília).
{bloco_estado}{bloco_personalidade}
COMO CONVERSAR:
- Fale natural e curto, como num WhatsApp real. Emojis com moderação (💕✨).
- Uma pergunta de cada vez. Varie as respostas, não repita frases prontas.
- Ajude a agendar, tire dúvidas de serviços/preços, e encante a cliente.

SERVIÇOS E PREÇOS:
{svcs_txt or '  (nenhum cadastrado)'}
PROFISSIONAIS:
{pros_txt or '  (nenhum cadastrado)'}
{hist_txt}
DATAS (use no formato YYYY-MM-DD quando precisar):
{ref_datas}
{bloco_disp}
AGENDAMENTO:
- Para agendar, descubra: serviço, profissional (se a cliente quiser escolher) e o dia/horário. Uma coisa de cada vez.
- Ofereça APENAS horários da lista de HORÁRIOS LIVRES acima. NUNCA ofereça um horário que não esteja nessa lista (pode já estar ocupado).
- Quando souber o serviço e o dia, ofereça 2 ou 3 horários livres e confirme a hora escolhida.
- Para registrar um agendamento confirmado, inclua no FINAL da mensagem (a cliente não vê): ##AGENDAR##cli_nome=NOME##svc_id=N##pro_id=N##data=YYYY-MM-DD##hora=HH:MM## — use os números [id=N] das listas acima. Faça isso UMA vez, só quando tiver todos os dados.
- Respostas curtas, no máximo 3-4 linhas."""

    return system

def detectar_intencao(msg):
    """Detecta intenção básica sem chamar a IA (economia de tokens)."""
    m = msg.lower().strip()
    saudacoes = ['oi', 'olá', 'ola', 'bom dia', 'boa tarde', 'boa noite', 'hey', 'hi']
    if any(m.startswith(s) for s in saudacoes) and len(m) < 20:
        return 'saudacao'
    if any(w in m for w in ['preço', 'preco', 'valor', 'quanto custa', 'tabela']):
        return 'preco'
    if any(w in m for w in ['agendar', 'marcar', 'horário', 'horario', 'disponível', 'disponivel', 'vaga']):
        return 'agendar'
    if any(w in m for w in ['cancelar', 'desmarcar', 'cancela']):
        return 'cancelar'
    if any(w in m for w in ['confirmar', 'confirmo', 'confirmado']):
        return 'confirmar'
    if any(w in m for w in ['obrigado', 'obrigada', 'valeu', 'tks', 'tchau', 'até']):
        return 'despedida'
    return 'geral'

def extrair_comandos(resposta):
    """Extrai comandos internos da resposta da IA.
    Formato: ##TIPO##chave=valor##chave=valor##...## (parâmetros separados por ##)."""
    comandos = []
    # Captura: ##TIPO## seguido de tudo até o fim da linha (ou fim do texto)
    pattern = r'##(\w+)##([^\n]*)'
    for m in re.finditer(pattern, resposta):
        tipo = m.group(1)
        params_raw = m.group(2)
        params = {}
        # Os parâmetros vêm separados por ## — ex: cli_nome=X##svc_id=1##pro_id=2##data=...##hora=...
        for p in re.split(r'#+', params_raw):
            p = p.strip()
            if '=' in p:
                k, v = p.split('=', 1)
                params[k.strip()] = v.strip().rstrip('#').strip()
        comandos.append({'tipo': tipo, 'params': params})
    # Limpar resposta de comandos internos (remove tudo a partir de ##TIPO##)
    resposta_limpa = re.sub(r'##\w+##[^\n]*', '', resposta).strip()
    return resposta_limpa, comandos

def responder(mensagem, salao, profissionais, servicos, historico=None, conversa=None, groq_key=None, personalidade=''):
    """
    Gera resposta da IA para uma mensagem do cliente.
    
    Returns: (resposta_texto, comandos_lista, erro)
    """
    intencao = detectar_intencao(mensagem)

    # Respostas automáticas para casos simples (sem gastar tokens)
    if intencao == 'despedida':
        nome = salao.get('nome', 'nós')
        return f"Foi um prazer atender você! Até logo 😊\n— Equipe {nome} 💕", [], None

    # Montar histórico de conversa
    msgs = []
    tem_historico = bool(conversa and len(conversa) > 0)
    system = montar_sistema(salao, profissionais, servicos, historico, personalidade, em_andamento=tem_historico)
    msgs.append({'role': 'system', 'content': system})

    # Adicionar contexto da conversa anterior (até 8 mensagens)
    if tem_historico:
        for item in conversa[-8:]:
            msgs.append({'role': item['role'], 'content': item['content']})

    msgs.append({'role': 'user', 'content': mensagem})

    resposta, erro = groq_chat(msgs, groq_key=groq_key)
    if erro:
        return None, [], erro

    resposta_limpa, comandos = extrair_comandos(resposta)
    return resposta_limpa, comandos, None

