# Frontend — Dashboard de Indicadores de Riesgo Legal
#
# Multi-stage: build con Node y servicio con nginx. nginx es ligero (~50 MB de
# memoria en reposo) y sirve el bundle estático sin necesidad de un runtime de
# Node en producción.

# ---------- Etapa 1: build ----------
FROM node:22-alpine AS build

WORKDIR /build

# Se copian solo los manifiestos para aprovechar la caché de capas: mientras las
# dependencias no cambien, este paso no se vuelve a ejecutar.
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

# ---------- Etapa 2: servicio ----------
FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

# nginx:alpine incluye wget (BusyBox) para el healthcheck.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
