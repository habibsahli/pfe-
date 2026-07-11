from app.services.ollama_client import ollama_client

try:
    print('Ollama host:', ollama_client.base_url)
    models = ollama_client.list_models()
    print('Available models:', models)
except Exception as e:
    print('Error checking ollama:', e)