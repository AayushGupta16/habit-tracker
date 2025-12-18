.PHONY: run stop

# Run the backend server (blocking)
run:
	uv run uvicorn main:app --reload

# Run the backend server in the background
start:
	uv run uvicorn main:app --reload > server.log 2>&1 & echo "Server started with PID $$!"

# Stop the backend server
stop:
	pkill -f "uvicorn main:app" || echo "No server running"

