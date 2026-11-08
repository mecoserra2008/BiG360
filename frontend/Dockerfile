# BiG Equity 360 frontend dev server (OPTIONAL docker path; native: run_frontend.ps1).
# Build context = frontend/:   docker build -t big360-frontend frontend
# Only sources are copied (never the host's node_modules, which hold Windows-native binaries).
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY public ./public
COPY src ./src
ENV VITE_USE_MOCK=false \
    VITE_API_URL=http://localhost:8000
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
