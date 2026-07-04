from django.shortcuts import render, redirect
from django.http import JsonResponse
import os
from google import genai

from django.contrib import auth
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from .models import Chat, Conversation

from django.utils import timezone

def ask_gemini_title(message):
    try:
        api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
        client = genai.Client(api_key=api_key)
        prompt = f"Crie um título muito curto (máximo 4 palavras) que resuma esta mensagem: '{message}'"
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip().replace('"', '').replace("'", "")
    except Exception:
        return "Nova Conversa"

def ask_gemini(message):
    try:
        # Tenta usar a chave 2 que verificamos ser válida
        api_key = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY_3")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=message,
        )
        return response.text.strip()
    except Exception as e:
        return f"Error connecting to Gemini: {str(e)}"

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
        
        if conv_id:
            try:
                conversation = Conversation.objects.get(id=conv_id, user=request.user)
                conversation.updated_at = timezone.now()
                conversation.save()
            except Conversation.DoesNotExist:
                return JsonResponse({'error': 'Conversation not found'}, status=404)
        else:
            title = ask_gemini_title(message)
            conversation = Conversation(user=request.user, title=title)
            conversation.save()
            
        response = ask_gemini(message)

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
