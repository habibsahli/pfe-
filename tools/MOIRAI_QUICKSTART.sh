#!/bin/bash
# Quick start guide for Moirai local service

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║          Moirai Local Forecasting Service Setup            ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}✓ Configuration Summary${NC}"
echo "  Service Name:        fibre_moirai"
echo "  Container Port:      8001"
echo "  API Endpoint:        http://moirai:8001 (internal)"
echo "  Local Dev URL:       http://localhost:8001"
echo "  Model:               Salesforce/moirai-1.1-R-small"
echo "  GPU Support:         Auto-detected (CUDA if available)"
echo ""

echo -e "${BLUE}✓ Files Created/Modified${NC}"
echo "  tools/moirai_server.py                 (FastAPI HTTP service)"
echo "  tools/moirai_requirements.txt           (Python dependencies)"
echo "  tools/MOIRAI_SERVICE_README.md          (Full documentation)"
echo "  docker/Dockerfile.moirai                (Container build)"
echo "  docker-compose.yml                      (Added moirai service + backend config)"
echo "  backend/app/core/config.py              (Default MOIRAI_API_URL set)"
echo ""

echo -e "${BLUE}✓ Next Steps${NC}"
echo ""
echo "  1. Build and start Moirai service:"
echo "     ${YELLOW}docker-compose up -d moirai${NC}"
echo ""
echo "  2. Wait for model to download (~2-5 min on first run)"
echo "     ${YELLOW}docker logs -f fibre_moirai${NC}"
echo ""
echo "  3. Verify health check:"
echo "     ${YELLOW}curl http://localhost:8001/health${NC}"
echo "     Expected: {\"status\":\"ok\",\"service\":\"moirai-forecast\"}"
echo ""
echo "  4. Restart backend to connect to Moirai:"
echo "     ${YELLOW}docker-compose restart backend${NC}"
echo ""
echo "  5. Run forecasting with Moirai:"
echo "     ${YELLOW}curl -X POST http://localhost:8000/api/training \\${NC}"
echo "     ${YELLOW}  -H 'Content-Type: application/json' \\${NC}"
echo "     ${YELLOW}  -d '{\"session_id\":\"test\",\"models\":[\"moirai\"],\"horizon\":6}'${NC}"
echo ""

echo -e "${GREEN}✓ Architecture Overview${NC}"
echo "  • Backend → Moirai Service (HTTP POST to :8001/forecast)"
echo "  • Moirai Service → PyTorch Model (generates forecast samples)"
echo "  • Response includes: mean, median, quantile_0.1, quantile_0.9"
echo "  • No external API keys required (fully local)"
echo ""

echo -e "${YELLOW}ℹ️  Troubleshooting Tips${NC}"
echo "  • Slow first request? Model is loading (~30-60s). This is normal."
echo "  • Memory issues? Reduce num_samples or increase --shm-size"
echo "  • GPU not detected? Set CUDA_VISIBLE_DEVICES in docker-compose.yml"
echo "  • Check logs: docker logs fibre_moirai"
echo ""

echo -e "${GREEN}✓ Ready to deploy!${NC}"
echo ""
