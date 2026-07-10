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
    def _call_gcp_cloud_run_stream(cls, prompt, system_msg, user_id, session_id):
        url = "https://chatbot-bot-dev-ecengx7mxa-rj.a.run.app"
        try:
            token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
        except Exception as e:
            raise ValueError(f"GCP Token error: {e}")
            
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        if not session_id:
            res_sess = requests.post(f"{url}/apps/bot/users/{user_id}/sessions", headers=headers, json={}, timeout=10)
            res_sess.raise_for_status()
            session_id = res_sess.json().get('id')
            
        full_prompt = f"[{system_msg}]\n\n{prompt}" if system_msg else prompt
        
        payload = {
            "app_name": "bot",
            "user_id": user_id,
            "session_id": session_id,
            "new_message": {
                "role": "user",
                "parts": [{"text": full_prompt}]
            }
        }
        
        final_text = ""
        with requests.post(f"{url}/run_sse", headers=headers, json=payload, stream=True, timeout=60) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    decoded = line.decode('utf-8')
                    if decoded.startswith("data: "):
                        data_str = decoded[6:].strip()
                        if not data_str: continue
                        try:
                            data = json.loads(data_str)
                            
                            if "actions" in data and "artifactDelta" in data["actions"]:
                                artifacts = data["actions"]["artifactDelta"]
                                for art_name, art_version in artifacts.items():
                                    art_url = f"{url}/apps/bot/users/{user_id}/sessions/{session_id}/artifacts/{art_name}/versions/{art_version}"
                                    art_res = requests.get(art_url, headers=headers)
                                    if art_res.status_code == 200:
                                        import base64
                                        b64 = base64.b64encode(art_res.content).decode()
                                        mime = "image/png" if art_name.endswith(".png") else "image/jpeg"
                                        img_md = f"\n\n![{art_name}](data:{mime};base64,{b64})\n\n"
                                        final_text += img_md
                                        yield {"type": "text", "text": img_md}

                            parts = data.get("content", {}).get("parts", [])
                            for p in parts:
                                if "functionCall" in p:
                                    yield {"type": "tool_call", "name": p["functionCall"]["name"]}
                                if "functionResponse" in p:
                                    yield {"type": "tool_resp", "name": p["functionResponse"]["name"]}
                                if "text" in p:
                                    final_text += p["text"]
                                    yield {"type": "text", "text": p["text"]}
                        except Exception as e:
                            pass
                            
        if not final_text:
            raise ValueError("Resposta vazia da GCP")
            
        yield {"type": "done", "final_text": final_text, "session_id": session_id}

    @classmethod
    def _call_gemini_stream(cls, prompt, system_msg=None, chat_history=None, mcp_tools=None, mcp_connections=None):
        api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
        
        # If we don't have API keys, just fail gracefully so fallback isn't used
        if not api_key:
             yield {"type": "error", "error": "Gemini API key is missing."}
             return
             
        client = genai.Client(api_key=api_key)
        
        # Handle tools config for Gemini
        tools_config = []
        if mcp_tools:
            # We map MCP tool schemas to Gemini FunctionDeclarations
            from google.genai import types
            
            gemini_tools = []
            for tool in mcp_tools:
                # Basic mapping, in a real scenario you would parse the JSON Schema to Gemini types
                parameters_schema = tool.get("parameters", {})
                
                # Gemini doesn't fully support all JSON Schema features cleanly in FunctionDeclaration yet without some manual mapping, 
                # but we can pass it as a dict and let the SDK handle it internally or parse it to OpenAPI schema.
                
                # Para simplificar e garantir funcionamento imediato no Gemini 2.5:
                # Se o schema MCP tiver 'properties', mapeamos para object
                if "properties" in parameters_schema:
                    gemini_tools.append(types.FunctionDeclaration(
                        name=tool["name"],
                        description=tool["description"],
                        # Precisamos mapear para dict cru, o SDK do Gemini prefere dicionarios OpenAPI 3.0
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
        
        # We start the session (we don't use stream directly here to easily handle function calls)
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=full_prompt,
                config=types.GenerateContentConfig(tools=tools_config) if tools_config else None
            )
            
            # Check if Gemini wants to call a tool
            if response.function_calls:
                for function_call in response.function_calls:
                    tool_name = function_call.name
                    arguments = {k: v for k, v in function_call.args.items()}
                    
                    yield {"type": "tool_call", "name": tool_name}
                    
                    # Execute tool via MCP
                    tool_result_text = "Tool failed."
                    for conn in mcp_connections:
                        try:
                            mcp_client = MCPClient(conn.mcp_server.url)
                            # We inject the tenant_id explicitly using the tenant_id from connection
                            if conn.tenant_id:
                                arguments["tenant_id"] = conn.tenant_id
                            elif "tenant_id" not in arguments:
                                arguments["tenant_id"] = conn.access_token
                            
                            res = mcp_client.call_tool_sync(tool_name, arguments)
                            if res:
                                tool_result_text = res
                                break # Found the server that handled it
                        except Exception as e:
                            pass
                    
                    yield {"type": "tool_resp", "name": tool_name}
                    
                    # Send result back to Gemini
                    function_response_part = types.Part.from_function_response(
                        name=tool_name,
                        response={"result": tool_result_text}
                    )
                    
                    # Do a follow up call
                    follow_up_response = client.models.generate_content(
                         model='gemini-2.5-flash',
                         contents=[
                             types.Content(role="user", parts=[types.Part.from_text(full_prompt)]),
                             types.Content(role="model", parts=response.parts),
                             types.Content(role="user", parts=[function_response_part])
                         ]
                    )
                    
                    final_text = follow_up_response.text.strip()
                    yield {"type": "text", "text": final_text}
                    yield {"type": "done", "final_text": final_text, "session_id": None}
                    return

            # No tool called, just text
            final_text = response.text.strip()
            yield {"type": "text", "text": final_text}
            yield {"type": "done", "final_text": final_text, "session_id": None}
            
        except Exception as e:
            yield {"type": "error", "error": f"Gemini falhou: {str(e)}"}

    @classmethod
    def generate_stream(cls, prompt, system_msg=None, chat_history=None, user_id=None, gcp_session_id=None, mcp_connections=None):
        
        mcp_tools = []
        if mcp_connections:
            for conn in mcp_connections:
                mcp_client = MCPClient(conn.mcp_server.url)
                tools = mcp_client.get_tools_sync()
                if tools:
                    mcp_tools.extend(tools)
                    
            if mcp_tools:
                system_msg += "\n\nVocê tem acesso a ferramentas de Gestão Agrícola (MCP). IMPORTANTE: Ao usar qualquer ferramenta que exija o parâmetro 'tenant_id', você DEVE preenchê-lo ESTRITAMENTE com o token cadastrado pelo usuário. O backend tentará injetar o tenant_id da fazenda do usuário se você falhar, mas por favor forneça qualquer string como placeholder se for um campo obrigatório no schema."

        try:
            # Em um cenário real, injetaríamos os mcp_tools no payload do gcp_cloud_run.
            # Como você mencionou que podemos usar o Gemini Fallback para simplificar o MCP,
            # vamos forçar o uso do Gemini se houver integrações MCP ativas para garantir que o function calling funcione perfeitamente via SDK.
            if mcp_tools:
                 print("[AI Routing] Redirecionando para Gemini devido ao uso de ferramentas MCP locais.")
                 yield from cls._call_gemini_stream(prompt, system_msg, chat_history, mcp_tools, mcp_connections)
                 return
                 
            yield from cls._call_gcp_cloud_run_stream(prompt, system_msg, user_id, gcp_session_id)
        except Exception as e:
            print(f"[AI Fallback] GCP API falhou ({e}). Tentando Gemini...")
            yield {"type": "error", "error": f"GCP falhou: {e}. Usando fallback."}
            try:
                yield from cls._call_gemini_stream(prompt, system_msg, chat_history)
            except Exception as e3:
                yield {"type": "fatal", "error": f"Erro fatal em todos os provedores: {str(e3)}"}

    @classmethod
    def generate_title(cls, prompt):
        try:
            api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            return response.text.strip()
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
        message = request.POST.get('message')
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
            "Você é o TarsLabs WhiteLabel UI Agent, um assistente inteligente e prestativo.\n"
            "Se você tiver acesso a ferramentas de Gestão Agrícola (MCP), atue como a camada de interface entre o produtor rural e o sistema.\n"
            "SEJA SEMPRE prestativo, profissional e levemente coloquial, focado no campo.\n"
            "VOCÊ É UM PARSER DETERMINÍSTICO: Se faltarem dados vitais para uma ferramenta (ex: qual a gleba? qual o insumo?), "
            "NÃO ADIVINHE E NÃO INVENTE DADOS. Pergunte primeiro ao usuário.\n"
            "Se o retorno da ferramenta indicar sucesso, avise o produtor rural de forma natural que a operação foi registrada no sistema."
        )

        def stream_generator():
            # Send initial metadata so UI knows the conversation ID
            yield f"data: {json.dumps({'type': 'metadata', 'conversation_id': conversation.id, 'is_new': (conv_id is None)})}\n\n"
            
            final_text = ""
            new_gcp_sid = gcp_sid
            
            for chunk in AIProvider.generate_stream(message, system_prompt, chat_history, request.user.username, gcp_sid, list(request.user.mcp_connections.all())):
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
                
                Chat.objects.create(user=request.user, conversation=conversation, message=message, response=final_text)

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
