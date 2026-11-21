from anthropic import Anthropic

client = Anthropic(
    api_key="YOUR_API_KEY"
)

response = client.messages.create(
    model="claude-sonnet-4",
    max_tokens=2000,
    messages=[
        {
            "role": "user",
            "content": "Summarise this AML policy..."
        }
    ]
)

print(response.content[0].text)