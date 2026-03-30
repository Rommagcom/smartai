# SmartAi Frontend Assistant

Standalone frontend application for the SmartAi API.

## Features
- Login and registration against SmartAi auth endpoints
- User-scoped chat (personal context by authenticated `user_id`)
- Message history refresh
- WebSocket live updates
- Local token persistence in browser storage

## API routes used
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/chat/send`
- `GET /api/v1/chat/messages`
- `GET /api/v1/ws-token`
- `WS /api/v1/chat/ws?token=...`

## Run locally
1. Install Node.js 20+ (includes npm).
2. Install dependencies:
   ```powershell
   cd frontend-assistant
   npm install
   ```
3. Start backend API on `http://127.0.0.1:8000`.
4. Run frontend:
   ```powershell
   npm run dev
   ```
5. Open `http://127.0.0.1:5173`.

By default, dev server proxies `/api` to backend `127.0.0.1:8000`.

## Optional environment
You can create `.env` in this folder:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
```

If not set, the app uses `/api/v1` and relies on Vite proxy.

## Docker deploy (Nginx)
Build image:

```powershell
cd frontend-assistant
docker build -t smartai-frontend .
```

Run container:

```powershell
docker run --rm -p 8080:80 smartai-frontend
```

Open `http://127.0.0.1:8080`.

Nginx config proxies `/api` requests to `http://host.docker.internal:8000`, so your API should be running on host machine port `8000`.
