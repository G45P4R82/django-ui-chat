from django.shortcuts import render, redirect
from django.http import JsonResponse
import os
import requests
from google import genai

from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from .models import Chat, Conversation

from django.utils import timezone

class AIProvider:
    """Gerencia chamadas para diferentes APIs de IA com sistema de fallback"""
    
    @classmethod
    def _call_custom_api(cls, prompt, system_msg=None, chat_history=None):
        """Tenta chamar a API Customizada (ex: Groq)"""
        url = os.environ.get("CUSTOM_API_URL", "https://api.groq.com/openai/v1/chat/completions")
        token = os.environ.get("CUSTOM_API_TOKEN")
        model = os.environ.get("CUSTOM_API_MODEL", "llama3-8b-8192")
        
        if not token:
            raise ValueError("Token da Custom API ausente.")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        
        messages = []
        if system_msg:
            messages.append({"role": "system", "content": system_msg})
            
        if chat_history:
            # Pega as últimas 10 mensagens para não estourar o limite de tokens
            for chat in chat_history[-10:]:
                messages.append({"role": "user", "content": chat.message})
                messages.append({"role": "assistant", "content": chat.response})
                
        messages.append({"role": "user", "content": prompt})

        response = requests.post(url, headers=headers, json={"model": model, "messages": messages}, timeout=15)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()

    @classmethod
    def _call_gemini(cls, prompt, system_msg=None, chat_history=None):
        """Fallback para o Gemini"""
        api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
        client = genai.Client(api_key=api_key)
        
        # Para evitar erros de formatação de roles, construímos o histórico como um super-prompt no fallback
        full_prompt = f"[{system_msg}]\n\n" if system_msg else ""
        
        if chat_history:
            for chat in chat_history[-10:]:
                full_prompt += f"Usuário: {chat.message}\nAssistente: {chat.response}\n\n"
                
        full_prompt += f"Usuário: {prompt}\nAssistente:"
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=full_prompt,
        )
        return response.text.strip()

    @classmethod
    def generate_response(cls, prompt, system_msg=None, chat_history=None):
        """Tenta API Principal, faz fallback pro Gemini se falhar"""
        try:
            return cls._call_custom_api(prompt, system_msg, chat_history)
        except Exception as e:
            print(f"[AI Fallback] Custom API falhou ({e}). Tentando Gemini...")
            try:
                return cls._call_gemini(prompt, system_msg, chat_history)
            except Exception as gemini_e:
                return f"Erro fatal em todos os provedores de IA: {str(gemini_e)}"


def ask_gemini_title(bot_response):
    system_prompt = "Você é um classificador. Responda APENAS com o título solicitado, sem aspas e sem formatação."
    user_prompt = f"Crie um título muito curto (máximo 4 palavras) que resuma este texto: '{bot_response}'"
    title = AIProvider.generate_response(user_prompt, system_prompt)
    
    if "Erro fatal" in title:
        return "Nova Conversa"
    return title.replace('"', '').replace("'", "")

def ask_gemini(message, chat_history=None):
    system_prompt = "Você é o assistente oficial e inteligente do Governo do Estado de Mato Grosso. Seja educado, prestativo e forneça informações governamentais quando necessário."
    return AIProvider.generate_response(message, system_prompt, chat_history)

# Create your views here.
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
        conversation = None
        
        if conv_id:
            try:
                conversation = Conversation.objects.get(id=conv_id, user=request.user)
                # Pega as mensagens dessa conversa para mandar de contexto
                chat_history = list(conversation.messages.all().order_by('created_at'))
                conversation.updated_at = timezone.now()
                conversation.save()
            except Conversation.DoesNotExist:
                return JsonResponse({'error': 'Conversation not found'}, status=404)
        
        # Gera a resposta do bot primeiro, passando o histórico de contexto
        response = ask_gemini(message, chat_history)
        
        if not conv_id:
            # Gera o título usando a resposta do bot (como é uma conversa nova, não tem histórico)
            title = ask_gemini_title(response)
            conversation = Conversation(user=request.user, title=title)
            conversation.save()

        chat = Chat(user=request.user, conversation=conversation, message=message, response=response, created_at=timezone.now())
        chat.save()
        
        return JsonResponse({
            'message': message, 
            'response': response, 
            'conversation_id': conversation.id,
            'is_new': conv_id is None,
            'title': conversation.title
        })
        
    return render(request, 'chatbot.html', {
        'chats': chats,
        'conversations': conversations,
        'active_conversation': active_conversation
    })

@login_required(login_url='login')
def profile(request):
    return render(request, 'profile.html')


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
            return render(request, 'login.html', {'error_message': error_message})
    else:
        return render(request, 'login.html')

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
                return render(request, 'register.html', {'error_message': error_message})
        else:
            error_message = 'Password dont match'
            return render(request, 'register.html', {'error_message': error_message})
    return render(request, 'register.html')

def logout(request):
    auth.logout(request)
    return redirect('login')
