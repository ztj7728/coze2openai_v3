from flask import Flask, request, Response, stream_with_context, jsonify
import requests
import json
import time

app = Flask(__name__)

# 添加 CORS 响应头
@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

# Coze API 地址及默认 user_id
COZE_API_URL = "https://api.coze.cn/v3/chat"
DEFAULT_COZE_USER_ID = "milo"

@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def chat_completions():
    if request.method == "OPTIONS":
        return Response(status=200)
        
    # 获取 OpenAI API 请求体
    openai_request = request.get_json() or {}
    messages = openai_request.get("messages", [])
    stream = openai_request.get("stream", False)

    # 从请求体中获取 model 作为 COZE_BOT_ID
    coze_bot_id = openai_request.get("model")
    if not coze_bot_id:
        return jsonify({"error": "Missing parameter: model (COZE_BOT_ID)"}), 400

    # 支持从请求体中传入 conversation_id 用于上下文保持
    conversation_id = openai_request.get("conversation_id")

    # 仅从 Authorization 头中提取 Bearer Token 作为 coze_api_key
    auth_header = request.headers.get("Authorization")
    if not (auth_header and auth_header.startswith("Bearer ")):
        return jsonify({"error": "Missing or invalid Authorization header"}), 400
    coze_api_key = auth_header.split(" ")[1]

    # 将所有 OpenAI 消息转换为 Coze API 格式
    additional_messages = []
    for msg in messages:
        additional_messages.append({
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "content_type": "text"
        })

    # 构造请求体，支持传入 conversation_id 保持上下文
    coze_payload = {
        "bot_id": coze_bot_id,
        "user_id": DEFAULT_COZE_USER_ID,
        "stream": stream,
        "additional_messages": additional_messages
    }
    if conversation_id:
        coze_payload["conversation_id"] = conversation_id

    coze_headers = {
        "Authorization": f"Bearer {coze_api_key}",
        "Content-Type": "application/json"
    }

    # 调用 Coze API，并启用流式响应
    coze_response = requests.post(COZE_API_URL, json=coze_payload, headers=coze_headers, stream=True)

    def generate():
        current_event = None
        for line in coze_response.iter_lines():
            if line:
                try:
                    decoded_line = line.decode("utf-8")
                    if decoded_line.startswith("event:"):
                        current_event = decoded_line.split(":", 1)[1].strip()
                    elif decoded_line.startswith("data:"):
                        data_part = decoded_line.split(":", 1)[1].strip()
                        if data_part == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        event_data = json.loads(data_part)
                        
                        # 处理消息完成事件
                        if current_event == "conversation.message.completed":
                            message_type = event_data.get("type")
                            
                            # 处理函数调用
                            if message_type == "function_call":
                                try:
                                    function_data = json.loads(event_data.get("content", "{}"))
                                    chunk = {
                                        "id": "chatcmpl-xxxxx",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": "coze-proxy",
                                        "choices": [{
                                            "delta": {
                                                "function_call": {
                                                    "name": function_data.get("name", ""),
                                                    "arguments": json.dumps(function_data.get("arguments", {}))
                                                }
                                            },
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }
                                    yield "data: " + json.dumps(chunk) + "\n\n"
                                except json.JSONDecodeError:
                                    print("Error parsing function_call content")
                                    
                            # 处理工具响应
                            elif message_type == "tool_response":
                                chunk = {
                                    "id": "chatcmpl-xxxxx",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": "coze-proxy",
                                    "choices": [{
                                        "delta": {
                                            "tool_response": event_data.get("content", "")
                                        },
                                        "index": 0,
                                        "finish_reason": None
                                    }]
                                    }
                                yield "data: " + json.dumps(chunk) + "\n\n"
                                
                        elif current_event == "conversation.message.delta":
                            # 处理普通的文本消息
                            content = event_data.get("content", "")
                            reasoning_content = event_data.get("reasoning_content", "")
                            delta_data = {}
                            if content:
                                delta_data["content"] = content
                            if reasoning_content:
                                delta_data["reasoning_content"] = reasoning_content
                            if delta_data:
                                chunk = {
                                    "id": "chatcmpl-xxxxx",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": "coze-proxy",
                                    "choices": [{
                                        "delta": delta_data,
                                        "index": 0,
                                        "finish_reason": None
                                    }]
                                }
                                yield "data: " + json.dumps(chunk) + "\n\n"
                                
                        # 当消息完全结束时
                        if current_event == "conversation.chat.completed":
                            chunk = {
                                "id": "chatcmpl-xxxxx",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "coze-proxy",
                                "choices": [{
                                    "delta": {},
                                    "index": 0,
                                    "finish_reason": "stop"
                                }]
                            }
                            yield "data: " + json.dumps(chunk) + "\n\n"
                except Exception as e:
                    print("Error processing line:", e)
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(port=5000)

