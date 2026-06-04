#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Musa SaaS — Motor de IA para WhatsApp
Usa Groq (gratuito) com Llama 3 para responder clientes
"""

import os, json, re, datetime
import urllib.request, urllib.error

GROQ_URL     = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL   = 'llama-3.3-70b-versatile'  # qualidade superior, gratuito (1000 msgs/dia)

def _get_groq_key():
    return os.environ.get('GROQ_API_KEY', '')

def _hoje_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).date()

def _hora_br():
    tz = datetime.timezone(datetime.timedelta(hours=-3))
    return datetime.datetime.now(tz).strftime('%H:%M')

def groq_chat(messages, max_tokens=600, groq_key=None):
    """Chama a API do Groq usando o SDK oficial (evita bloqueio Cloudflare 1010)."""
    if not groq_key:
        groq_key = _get_groq_key()
    if not groq_key:
        return None, "GROQ_API_KEY não configurada"

    # Método 1: SDK oficial do Groq (recomendado — não é bloqueado pelo Cloudflare)
    try:
        from groq import Groq
        client = Groq(api_key=groq_key)
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.4,
        )
        return completion.choices[0].message.content.strip(), None
    except ImportError:
        pass  # SDK não instalado, tentar requests
    except Exception as ex:
        # Erro de API (chave inválida, rate limit, etc)
        return None, str(ex)

    # Método 2: fallback via requests
    try:
        import requests
        resp = requests.post(
            GROQ_URL,
            json={'model': GROQ_MODEL, 'messages': messages,
                  'max_tokens': max_tokens, 'temperature': 0.4},
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + groq_key},
            timeout=20
        )
        if resp.status_code != 200:
            return None, 'HTTP ' + str(resp.status_code) + ': ' + resp.text[:200]
        data = resp.json()
        return data['choices'][0]['message']['content'].strip(), None
    except Exception as ex:
        return None, str(ex)

def montar_sistema(salao, profissionais, servicos, historico_cliente=None, personalidade=''):
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

    # Personalidade customizada pelo dono do salão (tem prioridade)
    bloco_personalidade = ''
    if personalidade and personalidade.strip():
        bloco_personalidade = f"""
PERSONALIDADE E INSTRUÇÕES DO SALÃO (definidas pelo dono — siga com prioridade):
{personalidade.strip()}
"""

    system = f"""Você é a recepcionista virtual do {nome_salao}. Seu nome combina com o salão e você atende pelo WhatsApp como se fosse uma pessoa real da equipe — calorosa, atenciosa e que faz o cliente se sentir especial.
Hoje é {dia_sem}, {hoje.strftime('%d/%m/%Y')} — {hora} (horário de Brasília).
{bloco_personalidade}

COMO VOCÊ CONVERSA (muito importante):
- Fale como uma pessoa de verdade, não como um robô. Nada de respostas engessadas.
- Seja calorosa e próxima: use o nome do cliente quando souber, demonstre que se importa.
- Acolha primeiro, resolva depois. Se a cliente parece animada para um evento, comemore junto ("que delícia, vai ficar linda! 💕"). Se parece com pressa, seja ágil.
- Faça UMA pergunta de cada vez, nunca várias juntas — conversa flui melhor assim.
- Frases curtas e naturais, como num WhatsApp real. Emojis com moderação e carinho (💕✨💇‍♀️), sem exagero.
- Varie suas respostas. Nunca repita a mesma frase pronta toda hora.
- Se não souber algo, seja honesta: "deixa eu confirmar isso pra você certinho 😊".

SEU EQUILÍBRIO (faça os três bem):
1. AGENDAR com fluidez — guie a cliente sem burocracia
2. TIRAR DÚVIDAS sobre serviços e preços com clareza e simpatia
3. ENCANTAR — cada conversa deve deixar a cliente com vontade de voltar

DADOS DO SALÃO:
Nome: {nome_salao}
{('Endereço: ' + end_salao) if end_salao else ''}
{('Telefone: ' + tel_salao) if tel_salao else ''}

SERVIÇOS E PREÇOS:
{svcs_txt or '  (sem serviços cadastrados)'}

PROFISSIONAIS:
{pros_txt or '  (sem profissionais cadastrados)'}
{hist_txt}
COMO CONDUZIR:
- Saudação ("oi", "olá"): cumprimente com calor, diga o nome do salão e pergunte de forma aberta como pode ajudar. Se a cliente já é conhecida (tem histórico), demonstre que lembra dela.
- Para agendar: descubra com leveza o serviço desejado, se tem profissional de preferência, e o melhor dia/horário — uma coisa de cada vez.
- Para preço: informe o valor com simpatia e, se fizer sentido, sugira um serviço que combina.
- Para cancelar/remarcar: confirme com gentileza o nome e o horário, sem fazer a cliente se sentir mal por cancelar.
- NUNCA invente nem prometa um horário específico sem confirmar no sistema. Diga que vai verificar a agenda.
- Mantenha respostas com no máximo 3-4 linhas. Assine de forma calorosa como: Equipe {nome_salao} 💕 (não precisa assinar toda mensagem, só quando fizer sentido encerrar).

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
    system = montar_sistema(salao, profissionais, servicos, historico, personalidade)
    msgs.append({'role': 'system', 'content': system})

    # Adicionar contexto da conversa anterior (últimas 6 mensagens)
    if conversa:
        for item in conversa[-6:]:
            msgs.append({'role': item['role'], 'content': item['content']})

    msgs.append({'role': 'user', 'content': mensagem})

    resposta, erro = groq_chat(msgs, groq_key=groq_key)
    if erro:
        return None, [], erro

    resposta_limpa, comandos = extrair_comandos(resposta)
    return resposta_limpa, comandos, None

