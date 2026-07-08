from django.db import models
from django.contrib.auth.models import User

class MCPServer(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    url = models.URLField(help_text="URL for the MCP Server SSE endpoint")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class UserMCPConnection(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mcp_connections')
    mcp_server = models.ForeignKey(MCPServer, on_delete=models.CASCADE)
    access_token = models.CharField(max_length=512)
    is_connected = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'mcp_server')

    def __str__(self):
        return f"{self.user.username} - {self.mcp_server.name}"

# Create your models here.
class Conversation(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=255, default="Nova Conversa")
    gcp_session_id = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title

class Chat(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, null=True, blank=True, related_name='messages')
    message = models.TextField()
    response = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username}: {self.message}'