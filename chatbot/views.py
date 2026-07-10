from django.shortcuts import render, redirect
from django.http import JsonResponse, StreamingHttpResponse
import os
import requests
import subprocess
import json
from google import genai

from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from .models import Chat, Conversation, MCPServer, UserMCPConnection
from .mcp_client import MCPClient

from django.utils import timezone

class AIProvider:
    @classmethod
    def _execute_mcp_tool(cls, tool_name, arguments, mcp_connections):
        tool_result_text = "Tool failed."
        if not mcp_connections:
            return tool_result_text

        for conn in mcp_connections:
            try:
                mcp_client = MCPClient(conn.mcp_server.url)
                if conn.tenant_id:
                    arguments["tenant_id"] = conn.tenant_id
                elif "tenant_id" not in arguments:
                    arguments["tenant_id"] = conn.access_token
                
                res = mcp_client.call_tool_sync(tool_name, arguments)
                if res:
                    return res
            except Exception as e:
                print(f"[MCP Error] Failed on server {conn.mcp_server.name}: {e}")
                
        return tool_result_text

    @classmethod
    def generate_stream(cls, prompt, system_msg=None, chat_history=None, user_id=None, gcp_session_id=None, mcp_connections=None):
        api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
        
        if not api_key:
             yield {"type": "error", "error": "As chaves de API do Gemini não estão configuradas (.env)."}
             return
             
        client = genai.Client(api_key=api_key)
        
        # Injetar regras e tools do MCP
        tools_config = []
        mcp_tools = []
        
        if mcp_connections:
            for conn in mcp_connections:
                try:
                    mcp_client = MCPClient(conn.mcp_server.url)
                    fetched_tools = mcp_client.get_tools_sync()
                    if fetched_tools:
                        mcp_tools.extend(fetched_tools)
                except Exception as e:
                    print(f"[MCP Error] Failed to fetch tools: {e}")
                    
            if mcp_tools:
                system_msg += "\n\nVocê tem acesso a ferramentas de Gestão Agrícola (MCP). IMPORTANTE: Ao usar qualquer ferramenta que exija o parâmetro 'tenant_id', você DEVE preenchê-lo ESTRITAMENTE com o token cadastrado pelo usuário."
                
                from google.genai import types
                gemini_tools = []
                for tool in mcp_tools:
                    parameters_schema = tool.get("parameters", {})
                    
                    # Gemini API doesn't support 'additionalProperties' or 'additional_properties' in its OpenAPI schema strict validation
                    if isinstance(parameters_schema, dict):
                        # Remove additionalProperties if it exists
                        if "additionalProperties" in parameters_schema:
                            del parameters_schema["additionalProperties"]
                        if "additional_properties" in parameters_schema:
                            del parameters_schema["additional_properties"]
                            
                    if "properties" in parameters_schema:
                        gemini_tools.append(types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                            parameters=parameters_schema
                        ))
                    else:
                        gemini_tools.append(types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                        ))
                if gemini_tools:
                     tools_config = [types.Tool(function_declarations=gemini_tools)]

        full_prompt = f"[{system_msg}]\n\n" if system_msg else ""
        if chat_history:
            for chat in chat_history[-10:]:
                full_prompt += f"Usuário: {chat.message}\nAssistente: {chat.response}\n\n"
        full_prompt += f"Usuário: {prompt}\nAssistente:"
        
        try:
            from google.genai import types
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=full_prompt,
                config=types.GenerateContentConfig(tools=tools_config) if tools_config else None
            )
            
            if response.function_calls:
                for function_call in response.function_calls:
                    tool_name = function_call.name
                    arguments = {k: v for k, v in function_call.args.items()}
                    
                    yield {"type": "tool_call", "name": tool_name}
                    
                    tool_result_text = cls._execute_mcp_tool(tool_name, arguments, mcp_connections)
                    
                    yield {"type": "tool_resp", "name": tool_name}
                    
                    function_response_part = types.Part.from_function_response(
                        name=tool_name,
                        response={"result": tool_result_text}
                    )
                    
                    follow_up_response = client.models.generate_content(
                         model='gemini-2.5-flash',
                         contents=[
                             types.Content(role="user", parts=[types.Part.from_text(text=full_prompt)]),
                             types.Content(role="model", parts=response.parts),
                             types.Content(role="user", parts=[function_response_part])
                         ]
                    )
                    
                    final_text = follow_up_response.text.strip() if follow_up_response.text else ''
                    yield {"type": "text", "text": final_text}
                    yield {"type": "done", "final_text": final_text, "session_id": None}
                    return

            final_text = response.text.strip() if response.text else ''
            yield {"type": "text", "text": final_text}
            yield {"type": "done", "final_text": final_text, "session_id": None}
            
        except Exception as e:
            yield {"type": "error", "error": f"Erro de processamento da IA: {str(e)}"}

    @classmethod
    def generate_title(cls, prompt):
        try:
            api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            return response.text.strip() if response.text else 'Nova Conversa'
        except:
            return "Nova Conversa"

def ask_gemini_title(bot_response):
    user_prompt = f"Crie um título muito curto (máximo 4 palavras) que resuma este texto: '{bot_response}'. Responda apenas o título, sem aspas."
    title = AIProvider.generate_title(user_prompt)
    return title.replace('"', '').replace("'", "")

@login_required(login_url='login')
def chatbot(request, conversation_id=None):
    conversations = Conversation.objects.filter(user=request.user).order_by('-updated_at')
    
    if conversation_id:
        try:
            active_conversation = Conversation.objects.get(id=conversation_id, user=request.user)
            chats = active_conversation.messages.all().order_by('created_at')
        except Conversation.DoesNotExist:
            return redirect('chatbot')
    else:
        active_conversation = None
        chats = []

    if request.method == 'POST':
        raw_message = request.POST.get('message')
        conv_id = request.POST.get('conversation_id')
        
        chat_history = []
        if conv_id:
            try:
                conversation = Conversation.objects.get(id=conv_id, user=request.user)
                chat_history = list(conversation.messages.all().order_by('created_at'))
            except Conversation.DoesNotExist:
                return JsonResponse({'error': 'Conversation not found'}, status=404)
        else:
            title = "Nova Conversa"
            conversation = Conversation.objects.create(user=request.user, title=title)
            conv_id = conversation.id

        gcp_sid = conversation.gcp_session_id
        
        system_prompt = (
            f"O nome do usuário é {request.user.username}. A data de hoje é {timezone.localtime().strftime('%d/%m/%Y')} e agora são {timezone.localtime().strftime('%H:%M')}.\n"
            "Se o usuário disser 'Olá' ou iniciar a conversa, cumprimente-o usando 'Bom dia/Boa tarde/Boa noite, [NOME]!'. Em seguida, faça um comentário curto, engajador e direto sobre gestão inteligente de fazendas, safras ou produtividade no campo, no estilo da TarsLabs.\n"
            "JAMAIS mencione governo, estado ou Mato Grosso. Você NÃO É um assistente do governo.\n"
            "Você é o TarsLabs WhiteLabel UI Agent, um assistente inteligente e prestativo focado no campo.\n"
            "Se você tiver acesso a ferramentas de Gestão Agrícola (MCP), atue como a camada de interface entre o produtor rural e o sistema.\n"
            "SEJA SEMPRE prestativo, profissional e levemente coloquial.\n"
            "COMO AGIR COM INTENÇÕES DE USUÁRIO: Se o usuário pedir para registrar, iniciar, encerrar ou cadastrar algo (ex: 'plantei soja', 'comprei adubo'), "
            "NÃO CHAME NENHUMA FERRAMENTA AINDA. Primeiro, extraia todas as informações do texto dele. USE A DATA DE HOJE injetada neste prompt para deduzir expressões como 'hoje', 'ontem' ou 'amanhã'. NUNCA peça para ele confirmar a data se ele já disse 'hoje'.\n"
            "Traduza termos técnicos para a linguagem do campo (ex: não diga 'ID da Gleba', use 'número da gleba').\n"
            "Organize os dados de forma clara (mostrando o que você entendeu), indique quais dados estão faltando (ex: o número da gleba exato se ele disse apenas um apelido) e PEÇA APROVAÇÃO do usuário ANTES de rodar qualquer comando no sistema.\n"
            "Você SÓ pode executar comandos (ferramentas) no sistema APÓS o usuário confirmar o resumo organizado que você enviou.\n"
            "Se o retorno da ferramenta indicar sucesso, avise o produtor rural de forma natural que a operação foi registrada no sistema."
        )

        db_message = raw_message
        message_to_ai = raw_message

        if raw_message == '__PROACTIVE_START__':
            db_message = "Resumo Diário Automático"
            has_mcp = request.user.mcp_connections.exists()
            if has_mcp:
                message_to_ai = (
                    "AJA PROATIVAMENTE AGORA (Modo de Inicialização do App). "
                    "1. Dê 'Bom dia/Boa tarde/Boa noite' para mim. "
                    "2. CHAME IMEDIATAMENTE a ferramenta `mcp_consultar_safras_ativas` (injetando o tenant_id). "
                    "3. Apresente um resumo rápido e empolgante sobre o status da minha fazenda. "
                    "4. Finalize perguntando se quero registrar alguma atividade ou despesa hoje."
                )
            else:
                message_to_ai = (
                    "AJA PROATIVAMENTE AGORA (Modo de Inicialização do App). "
                    "1. Dê 'Bom dia/Boa tarde/Boa noite' para mim. "
                    "2. Diga que percebeu que eu ainda não conectei o sistema de gestão agrícola na aba 'Integrações MCP', e que você precisa disso para gerar os relatórios diários automáticos da minha fazenda."
                )

        def stream_generator():
            # Send initial metadata so UI knows the conversation ID
            yield f"data: {json.dumps({'type': 'metadata', 'conversation_id': conversation.id, 'is_new': (conv_id is None)})}\n\n"
            
            final_text = ""
            new_gcp_sid = gcp_sid
            
            for chunk in AIProvider.generate_stream(message_to_ai, system_prompt, chat_history, request.user.username, gcp_sid, list(request.user.mcp_connections.all())):
                if chunk["type"] == "done":
                    final_text = chunk["final_text"]
                    if chunk.get("session_id"):
                        new_gcp_sid = chunk["session_id"]
                else:
                    yield f"data: {json.dumps(chunk)}\n\n"
                    
            # After stream completes, save to DB
            if final_text:
                if not conversation.gcp_session_id and new_gcp_sid:
                    conversation.gcp_session_id = new_gcp_sid
                
                # Update title asynchronously if it was generic
                if conversation.title == "Nova Conversa":
                    conversation.title = ask_gemini_title(final_text)
                    yield f"data: {json.dumps({'type': 'title_updated', 'title': conversation.title})}\n\n"
                    
                conversation.updated_at = timezone.now()
                conversation.save()
                
                Chat.objects.create(user=request.user, conversation=conversation, message=db_message, response=final_text)

            yield "data: [DONE]\n\n"

        return StreamingHttpResponse(stream_generator(), content_type='text/event-stream')
        
    return render(request, 'chatbot/chatbot.html', {
        'chats': chats,
        'conversations': conversations,
        'active_conversation': active_conversation
    })

@login_required(login_url='login')
def profile(request):
    return render(request, 'chatbot/profile.html')

@login_required(login_url='login')
def integrations(request):
    servers = MCPServer.objects.filter(is_active=True)
    user_connections = UserMCPConnection.objects.filter(user=request.user)
    connected_server_ids = [conn.mcp_server.id for conn in user_connections]

    if request.method == 'POST':
        server_id = request.POST.get('server_id')
        action = request.POST.get('action')
        mcp_username = request.POST.get('mcp_username')
        mcp_password = request.POST.get('mcp_password')

        try:
            server = MCPServer.objects.get(id=server_id)
            if action == 'connect' and mcp_username and mcp_password:
                # Fazer requisição real pro /auth/login do servidor MCP
                auth_url = server.url.replace('/sse', '/auth/login')
                try:
                    res = requests.post(auth_url, json={"username": mcp_username, "password": mcp_password}, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        token = data.get('access_token')
                        tenant_id = data.get('tenant_id')
                        
                        if token and tenant_id:
                            UserMCPConnection.objects.update_or_create(
                                user=request.user,
                                mcp_server=server,
                                defaults={'access_token': token, 'tenant_id': tenant_id, 'is_connected': True}
                            )
                            return redirect('integrations')
                        else:
                            error_message = "Resposta inválida do servidor MCP."
                    else:
                         error_message = "Credenciais inválidas no sistema agrícola."
                except Exception as e:
                     error_message = f"Erro ao conectar no servidor: {str(e)}"
                     
                return render(request, 'chatbot/integrations.html', {
                    'servers': servers,
                    'connected_server_ids': connected_server_ids,
                    'user_connections': {conn.mcp_server.id: conn for conn in user_connections},
                    'error_message': error_message,
                    'error_server_id': server.id
                })
                
            elif action == 'disconnect':
                UserMCPConnection.objects.filter(user=request.user, mcp_server=server).delete()
            return redirect('integrations')
        except MCPServer.DoesNotExist:
            pass

    return render(request, 'chatbot/integrations.html', {
        'servers': servers,
        'connected_server_ids': connected_server_ids,
        'user_connections': {conn.mcp_server.id: conn for conn in user_connections}
    })

def login(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        
        # 1. Tenta autenticação unificada via Servidor MCP
        mcp_authenticated = False
        mcp_data = None
        server = MCPServer.objects.filter(is_active=True).first()
        
        if server:
            auth_url = server.url.replace('/sse', '/auth/login')
            try:
                res = requests.post(auth_url, json={"username": username, "password": password}, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if data.get('access_token') and data.get('tenant_id'):
                        mcp_authenticated = True
                        mcp_data = data
            except Exception as e:
                print(f"[MCP Auth Error] Falha ao contatar {auth_url}: {e}")

        if mcp_authenticated:
            # Cria ou atualiza o usuário no Django transparente para o usuário
            user, created = User.objects.get_or_create(username=username)
            user.set_password(password)
            user.save()
            
            # Autentica e loga
            user = auth.authenticate(request, username=username, password=password)
            if user:
                auth.login(request, user)
                # Salva a conexão para uso na IA
                UserMCPConnection.objects.update_or_create(
                    user=user,
                    mcp_server=server,
                    defaults={
                        'access_token': mcp_data['access_token'], 
                        'tenant_id': mcp_data['tenant_id'], 
                        'is_connected': True
                    }
                )
                return redirect('chatbot')

        # 2. Fallback: Autenticação padrão do Django (garante que o 'admin' continue funcionando)
        user = auth.authenticate(request, username=username, password=password)
        if user is not None:
            auth.login(request, user)
            return redirect('chatbot')
        else:
            error_message = 'Invalid username or password'
            return render(request, 'chatbot/login.html', {'error_message': error_message})
    else:
        return render(request, 'chatbot/login.html')

def register(request):
    if request.method == 'POST':
        username = request.POST['username']
        email = request.POST['email']
        password1 = request.POST['password1']
        password2 = request.POST['password2']

        if password1 == password2:
            try:
                user = User.objects.create_user(username, email, password1)
                user.save()
                auth.login(request, user)
                return redirect('chatbot')
            except:
                error_message = 'Error creating account'
                return render(request, 'chatbot/register.html', {'error_message': error_message})
        else:
            error_message = 'Password dont match'
            return render(request, 'chatbot/register.html', {'error_message': error_message})
    return render(request, 'chatbot/register.html')

def logout(request):
    auth.logout(request)
    return redirect('login')
