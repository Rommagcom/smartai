import ollama
import os

# Set your API key/token
# It is recommended to use environment variables for security
api_key = os.getenv("OLLAMA_API_KEY", "your_secret_token")

# Define the headers dictionary
headers = {
    "Authorization": f"Bearer {api_key}"
}

# Define the Ollama URL if it's not the default local one (e.g., if using a remote server or proxy)
ollama_url = "http://localhost:11434" # Change this if your URL is different

# Initialize the client with the custom headers
client = ollama.Client(host=ollama_url, headers=headers)

# Now you can make API calls as usual
try:
    response = client.generate(model='gpt-oss:120b-cloud', prompt='Why is the sky blue?')
    print(response['response'])
except Exception as e:
    print(f"An error occurred: {e}")
