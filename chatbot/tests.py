from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import Conversation, Chat

class ChatbotSystemTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpassword')
        
    def test_chatbot_view_authentication(self):
        # Should redirect if not logged in
        response = self.client.get('/')
        self.assertRedirects(response, '/login?next=/')
        
        # Should return 200 if logged in
        self.client.login(username='testuser', password='testpassword')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_markdown_rendering_classes(self):
        self.client.login(username='testuser', password='testpassword')
        conv = Conversation.objects.create(user=self.user, title='Test Conv')
        Chat.objects.create(
            user=self.user, 
            conversation=conv, 
            message='Test message', 
            response='**Bold Markdown**\n\n```python\nprint("Hello")\n```'
        )
        
        response = self.client.get(f'/c/{conv.id}/')
        content = response.content.decode('utf-8')
        
        # Check if the safe JSON data block and historical-msg div exist
        self.assertIn('raw-markdown-data', content)
        self.assertIn('historical-msg', content)
        # Check if the raw text is inside the JSON script tag
        self.assertIn('**Bold Markdown**', content)
        # Check if the JS logic for marked.parse exists
        self.assertIn('marked.parse(', content)
