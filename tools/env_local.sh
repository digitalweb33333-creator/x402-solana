#!/usr/bin/env bash
# Source-helper pour l'exécution LOCALE uniquement.
# Exporte les SECRETS (CDP, upstream, LLM) depuis le .env Base SANS jamais les
# écrire dans le dossier Solana, en EXCLUANT les valeurs spécifiques à Base
# (WALLET_ADDRESS, PUBLIC_BASE_URL). Les valeurs publiques Solana viennent du
# .env local (chargé par python-dotenv dans app/config.py).
#
# Usage :  source tools/env_local.sh
set -a
# shellcheck disable=SC1090
source <(grep -vE '^\s*#' /home/joachim/x402-endpoints/.env \
         | grep -vE '^(WALLET_ADDRESS|PUBLIC_BASE_URL|BUYER_PRIVATE_KEY)=' \
         | grep -E '=')
set +a
