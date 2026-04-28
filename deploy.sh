#!/usr/bin/env bash
set -euo pipefail

NAS_SSH="ssh -p 42069 -i ~/.ssh/id_ed25519 marcusg@192.168.1.166"
DEPLOY_DIR="/volume1/docker/kometa/app"
DOCKER="/var/packages/ContainerManager/target/usr/bin/docker"

echo "==> Pushing to Gitea..."
git push origin main

echo "==> Deploying to NAS..."
$NAS_SSH "
  set -euo pipefail
  cd $DEPLOY_DIR

  echo '--- Pulling latest from Gitea...'
  $DOCKER exec gitea git -C /tmp/kometa_repo pull 2>/dev/null || \
    $DOCKER exec gitea git clone http://marcusg:046ee22b21efc43fd55fe93e1e4b99a460245cee@localhost:3000/marcusg/kometa.git /tmp/kometa_repo

  $DOCKER cp gitea:/tmp/kometa_repo/. $DEPLOY_DIR/

  echo '--- Rebuilding container...'
  $DOCKER compose --env-file .env up -d --build

  echo '--- Done.'
  $DOCKER logs --tail=5 kometa
"
