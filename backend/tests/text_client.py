"""
Simple terminal text client — type a message, get a reply.
Maintains conversation memory across turns via thread_id.

Usage:
    python tests/text_client.py
    python tests/text_client.py --server http://localhost:8000
    python tests/text_client.py --system-prompt "You are a pirate."
    python tests/text_client.py --thread-id <existing-id>   # resume a session
"""
import argparse
import sys
import uuid
import httpx


def check_server(base_url: str):
    try:
        r = httpx.get(f"{base_url}/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"\nERROR: Cannot reach server at {base_url}")
        print(f"       {e}")
        print(f"\nStart the server first:\n  uvicorn main:app --reload\n")
        sys.exit(1)


def chat(base_url: str, message: str, system_prompt: str, thread_id: str) -> str:
    r = httpx.post(
        f"{base_url}/api/chat",
        json={"message": message, "system_prompt": system_prompt, "thread_id": thread_id},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["reply"]


def main():
    parser = argparse.ArgumentParser(description="Terminal text chat client")
    parser.add_argument("--server", default="http://localhost:8000", metavar="URL")
    parser.add_argument("--system-prompt", default="You are a helpful assistant.", dest="system_prompt")
    parser.add_argument("--thread-id", default=None, dest="thread_id", help="Resume an existing session")
    args = parser.parse_args()

    check_server(args.server)

    thread_id = args.thread_id or str(uuid.uuid4())
    print(f"Connected to {args.server}")
    print(f"Session   : {thread_id}  (pass --thread-id to resume)")
    print(f"Ctrl+C or 'exit' to quit\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye!")
            break

        try:
            reply = chat(args.server, user_input, args.system_prompt, thread_id)
            print(f"Bot: {reply}\n")
        except httpx.HTTPStatusError as e:
            print(f"Error {e.response.status_code}: {e.response.text}\n")
        except Exception as e:
            print(f"Error: {e}\n")


if __name__ == "__main__":
    main()
