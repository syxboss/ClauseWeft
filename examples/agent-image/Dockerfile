FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 ripgrep \
    && ln -s /usr/bin/python3 /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json /opt/agent-clis/
RUN npm ci --prefix /opt/agent-clis --omit=dev --ignore-scripts --no-fund --no-audit \
    && node /opt/agent-clis/node_modules/@anthropic-ai/claude-code/install.cjs \
    && ln -s /opt/agent-clis/node_modules/.bin/claude /usr/local/bin/claude \
    && ln -s /opt/agent-clis/node_modules/.bin/dsh /usr/local/bin/dsh \
    && npm cache clean --force \
    && /usr/local/bin/python3 --version && claude --version && dsh --version

USER node
