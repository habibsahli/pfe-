#!/bin/bash
set -e

echo "🚀 Starting Fibre Forecast System..."
echo "========================================="

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Step 1: Start Docker containers
echo -e "${BLUE}1. Starting Docker containers...${NC}"
# Drop any stale backend container first; compose can trip over removed image metadata
# when recreating an old backend container.
docker ps -aq --filter name='fibre_backend' | xargs -r docker rm -f > /dev/null 2>&1 || true
docker ps -aq --filter name='fibre_moirai' | xargs -r docker rm -f > /dev/null 2>&1 || true
docker-compose down --remove-orphans > /dev/null 2>&1 || true
docker-compose up -d --build --no-deps postgres etcd minio milvus ollama mlflow phoenix backend

# Wait for backend API to come up before checking the rest of the stack.
echo -e "${BLUE}2. Waiting for Backend API to be ready...${NC}"
for i in {1..45}; do
  if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Backend API is ready${NC}"
    break
  fi
  echo "Waiting... ($i/45)"
  sleep 2
done

# Step 3: Start the frontend UI
echo -e "${BLUE}3. Starting Frontend UI...${NC}"
docker-compose up -d --build --no-deps frontend

# Step 4: Wait for PostgreSQL to be ready
echo -e "${BLUE}4. Waiting for PostgreSQL to be ready...${NC}"
for i in {1..30}; do
  if docker exec fibre_postgres pg_isready -U admin > /dev/null 2>&1; then
    echo -e "${GREEN}✓ PostgreSQL is ready${NC}"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

# Step 5: Wait for Milvus to be ready
echo -e "${BLUE}5. Waiting for Milvus to be ready...${NC}"
for i in {1..30}; do
  if docker exec fibre_milvus curl -f http://localhost:9091/healthz > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Milvus is ready${NC}"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

# Step 6: Wait for Ollama to be ready
echo -e "${BLUE}6. Waiting for Ollama to be ready...${NC}"
for i in {1..30}; do
  if docker exec fibre_ollama curl -f http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Ollama is ready${NC}"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

# Step 7: Pull Ollama models
echo -e "${BLUE}7. Pulling Ollama models (this may take a few minutes)...${NC}"
docker exec fibre_ollama ollama pull bge-m3 2>/dev/null || echo "bge-m3 already cached"
docker exec fibre_ollama ollama pull llama3.2:3b 2>/dev/null || echo "llama3.2:3b already cached"
docker exec fibre_ollama ollama pull chronos 2>/dev/null || echo "chronos already cached"

echo -e "${GREEN}✓ Ollama models ready${NC}"

# Step 8: Wait for MLflow to be ready
echo -e "${BLUE}8. Waiting for MLflow to be ready...${NC}"
for i in {1..30}; do
  if curl -f http://localhost:5001 > /dev/null 2>&1; then
    echo -e "${GREEN}✓ MLflow is ready${NC}"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

# Step 9: Wait for Phoenix to be ready
echo -e "${BLUE}9. Waiting for Phoenix to be ready...${NC}"
for i in {1..30}; do
  if curl -f http://localhost:6006/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Phoenix is ready${NC}"
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

# Step 10: Verify all services
echo -e "${BLUE}10. Verifying services...${NC}"
echo -e "${YELLOW}Testing endpoints:${NC}"

echo -n "  PostgreSQL:      "
if nc -z localhost 5433 > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${RED}✗${NC}"
fi

echo -n "  Milvus:          "
if nc -z localhost 19530 > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${RED}✗${NC}"
fi

echo -n "  Ollama:          "
if nc -z localhost 11434 > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${RED}✗${NC}"
fi

echo -n "  MLflow:          "
if curl -sf http://localhost:5001 > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${RED}✗${NC}"
fi

echo -n "  Phoenix:         "
if curl -sf http://localhost:6006/health > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${RED}✗${NC}"
fi

echo -n "  Backend API:     "
if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
  echo -e "${GREEN}✓${NC}"
else
  echo -e "${YELLOW}⏳ Starting...${NC}"
fi

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}✅ System startup complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "📍 Access Points:"
echo -e "   🌐 Frontend UI:     ${BLUE}http://localhost:3000${NC}"
echo -e "   🔌 Backend API:     ${BLUE}http://localhost:8000${NC}"
echo -e "   📊 MLflow UI:       ${BLUE}http://localhost:5001${NC}"
echo -e "   📡 Phoenix Tracing: ${BLUE}http://localhost:6006${NC}"
echo -e "   🗂️  Milvus Attu:    ${BLUE}http://localhost:8001${NC}"
echo ""
echo "💡 Useful commands:"
echo "   View logs:       docker-compose logs -f backend"
echo "   Stop services:   docker-compose down"
echo "   Rebuild images:  docker-compose up -d --build"
echo ""
curl -f http://localhost:8000/health || echo "Backend not ready"
curl -f http://localhost:3000 || echo "Frontend not ready"
curl -f http://localhost:6006 || echo "Phoenix not ready"

echo "System ready"
echo "Backend API : http://localhost:8000"
echo "Frontend UI : http://localhost:3000"
echo "MLflow UI   : http://localhost:5001"
echo "Phoenix UI  : http://localhost:6006"
echo "Attu UI     : http://localhost:8001"
