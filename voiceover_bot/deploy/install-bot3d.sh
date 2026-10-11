#!/usr/bin/env bash
# Установка 3D-бота (bot3d.py) службой systemd на Ubuntu 22.04/24.04.
# Запуск на сервере:  bash install-bot3d.sh
# Повторный запуск = обновление кода (токены не спрашивает, если уже введены).
set -e

REPO="https://github.com/reshatmemetov01/---.git"
BRANCH="claude/festive-euler-ioure0"
DIR="/opt/pdd3d"
SVC="bot3d"

if [ "$(id -u)" != "0" ]; then echo "Запусти от root (или через sudo)"; exit 1; fi

echo "==> Ставлю системные пакеты..."
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip ffmpeg fonts-dejavu-core

echo "==> Качаю код..."
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --depth 1 origin "$BRANCH" && git -C "$DIR" reset --hard FETCH_HEAD
else
  git clone --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi
APP="$DIR/voiceover_bot"

echo "==> Ставлю Python-пакеты (пару минут)..."
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -U pip
"$DIR/venv/bin/pip" install -q "aiogram>=3.4,<4" edge-tts imageio-ffmpeg Pillow "numpy<2.3" "playwright>=1.40" yandex-speechkit
echo "==> Ставлю браузер Chromium для рендера..."
"$DIR/venv/bin/python" -m playwright install --with-deps chromium

ENV="$DIR/bot3d.env"
if [ ! -f "$ENV" ]; then
  echo
  echo "==> Настройка (вводимое не показывается на экране - это нормально)"
  read -r -s -p "Токен бота от @BotFather: " T; echo
  read -r -s -p "Ключ Яндекса (YANDEX_API_KEY): " K; echo
  read -r -p "Твой Telegram id (можно пусто, узнаешь через /start и впишешь потом): " O
  cat > "$ENV" <<EOF
BOT_TOKEN=$T
YANDEX_API_KEY=$K
BOT_OWNER=$O
YANDEX_SPEED=1.15
BOT3D_ENGINE=yandex3
BOT3D_VOICE=anton
BOT3D_OUT=$DIR/videos
PDD3D_SWIFTSHADER=1
PYTHONIOENCODING=utf-8
EOF
  chmod 600 "$ENV"
fi
mkdir -p "$DIR/videos"

echo "==> Создаю службу $SVC..."
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=PDD 3D video Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP
EnvironmentFile=$ENV
ExecStart=$DIR/venv/bin/python -u bot3d.py
Restart=always
RestartSec=5
# рендер тяжёлый: даём ему меньший приоритет, чтобы не мешал другим ботам/сайту
CPUWeight=50
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 3
systemctl --no-pager status "$SVC" | head -5
echo
echo "Готово! Напиши боту /start в Телеграме."
echo "Логи:        journalctl -u $SVC -f"
echo "Перезапуск:  systemctl restart $SVC"
echo "Настройки:   nano $ENV   (потом перезапуск)"
