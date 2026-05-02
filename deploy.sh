#!/usr/bin/env bash
set -e

cd /home/JR-Foxy

git fetch origin main
git reset --hard origin/main

docker compose up -d --build
docker compose ps
