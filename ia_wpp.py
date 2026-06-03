#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Musa SaaS — Motor de IA para WhatsApp
Usa Groq (gratuito) com Llama 3 para responder clientes
"""

import os, json, re, datetime
import urllib.request, urllib.error

GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_URL     = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL   = 'llama-3.1-8b-instant'  # rápido e gratuito

def _hoje_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).date()

def _hora_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).strftime('%H:%M')

def groq_chat(messages, max_tokens=600):
    """Chama a API do Groq."""
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY não configurada"
    payload = json.dumps({
        'model':      GROQ_MODEL,
        'messages':   messages,
        'max_tokens': max_tokens,
        'temperature': 0.4,
    }).encode('utf-8')
    req = urllib.request.Request(
        GROQ_URL,
        data=payload,
        headers={
            'Content-Type':  'application/json',
            'Authorization': 'Bearer ' + GROQ_API_KEY,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content'].strip(), None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        return None, 'HTTP ' + str(e.code) + ': ' + body[:200]
    except Exception as ex:
        return None, str(ex)

def montar_sistema(salao, profissionais, servicos, historico_cliente=None):
    """Monta o system prompt com contexto completo do salão."""
    hoje    = _hoje_br()
    hora    = _hora_br()
    dia_sem = ['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo'][hoje.weekday()]

    svcs_txt = ''
    for s in servicos:
        if s.get('ativo', 1):
            preco = f"R$ {float(s.get('preco',0)):.2f}".replace('.',',')
            dur   = s.get('duracao_min', 60)
            svcs_txt += f"  • {s['nome']} — {preco} ({dur} min)\n"

    pros_txt = ''
    for p in profissionais:
        if p.get('ativo', 1):
            cargo = p.get('cargo', '') or 'Profissional'
            pros_txt += f"  • {p['nome']} ({cargo})\n"

    hist_txt = ''
    if historico_cliente:
        hist_txt = '\nHISTÓRICO DO CLIENTE:\n'
        for h in historico_cliente[:5]:
            hist_txt += f"  • {h.get('data','')} — {h.get('svc_nome','?')} com {h.get('pro_nome','?')}\n"

    nome_salao = salao.get('nome', 'Salão')
    tel_salao  = salao.get('telefone', '')
    end_salao  = salao.get('endereco', '')

    system = f"""Você é a assistente virtual do {nome_salao}, um salão de beleza.
Hoje é {dia_sem}, {hoje.strftime('%d/%m/%Y')} — {hora} (horário de Brasília).

SEU PAPEL:
- Atender clientes via WhatsApp de forma simpática, rápida e profissional
- Informar serviços, preços e horários disponíveis
- Confirmar, reagendar ou cancelar agendamentos
- Sempre usar linguagem natural, amigável e com emojis moderados
- Nunca inventar informações — se não souber, diga que vai verificar
- Respostas curtas e objetivas (máximo 3-4 linhas por mensagem)
- Sempre assinar como: Equipe {nome_salao} 💕

DADOS DO SALÃO:
Nome: {nome_salao}
{('Endereço: ' + end_salao) if end_salao else ''}
{('Telefone: ' + tel_salao) if tel_salao else ''}

SERVIÇOS E PREÇOS:
{svcs_txt or '  (sem serviços cadastrados)'}

PROFISSIONAIS:
{pros_txt or '  (sem profissionais cadastrados)'}
{hist_txt}
INSTRUÇÕES ESPECIAIS:
- Para agendar: pergunte o serviço desejado, profissional preferido e melhor dia/hora
- Para cancelar: confirme nome e horário do agendamento
- Quando o cliente disser "oi", "olá" ou similar: cumprimente com o nome do salão e pergunte como pode ajudar
- Se perguntarem preço: liste os serviços relevantes com valores
- Horários disponíveis: responda que vai verificar e peça o dia preferido
- NUNCA prometa horário específico sem confirmação do sistema

COMANDOS INTERNOS (inclua no final da resposta quando necessário):
- Para verificar horários disponíveis: ##VERIFICAR_AGENDA##dia=YYYY-MM-DD##pro_id=N##
- Para confirmar agendamento: ##AGENDAR##cli_nome=NOME##svc_id=N##pro_id=N##data=YYYY-MM-DD##hora=HH:MM##
- Para cancelar: ##CANCELAR##ag_id=N##"""

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
    """Extrai comandos internos da resposta da IA."""
    comandos = []
    pattern = r'##(\w+)##([^#\n]*)##?'
    for m in re.finditer(pattern, resposta):
        tipo = m.group(1)
        params_raw = m.group(2)
        params = {}
        for p in params_raw.split('##'):
            if '=' in p:
                k, v = p.split('=', 1)
                params[k.strip()] = v.strip()
        comandos.append({'tipo': tipo, 'params': params})
    # Limpar resposta de comandos internos
    resposta_limpa = re.sub(r'##\w+##[^\n]*##?', '', resposta).strip()
    return resposta_limpa, comandos

def responder(mensagem, salao, profissionais, servicos, historico=None, conversa=None):
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
    system = montar_sistema(salao, profissionais, servicos, historico)
    msgs.append({'role': 'system', 'content': system})

    # Adicionar contexto da conversa anterior (últimas 6 mensagens)
    if conversa:
        for item in conversa[-6:]:
            msgs.append({'role': item['role'], 'content': item['content']})

    msgs.append({'role': 'user', 'content': mensagem})

    resposta, erro = groq_chat(msgs)
    if erro:
        return None, [], erro

    resposta_limpa, comandos = extrair_comandos(resposta)
    return resposta_limpa, comandos, None

