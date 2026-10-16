# drive-tube
Player YouTube leggero per il browser dell'auto (MJPEG + Web Audio API).

Un unico file Python: `yt-dlp` e `ffmpeg` trasformano lo stream YouTube in MJPEG (video) e MP3 (audio), il browser li riproduce su canvas e AudioContext con l'audio come orologio principale.

## Installazione su una macchina nuova
Serve una macchina Linux con `python3`, `git`, `ffmpeg` (con `libmp3lame`) e `yt-dlp`.

**Ubuntu / Debian**
```bash
sudo apt update && sudo apt install -y python3 git ffmpeg curl
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

**Amazon Linux 2023** (ffmpeg non è nei repository, si usa la build statica)
```bash
sudo dnf install -y git python3 curl tar xz
cd /tmp && curl -LO https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz && sudo cp ffmpeg-*-static/ffmpeg /usr/local/bin/
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```
Le versioni recenti di yt-dlp possono chiedere un runtime JavaScript per YouTube: se nel log compaiono avvisi, installa anche `nodejs` (o Deno).

**Scarica e avvia**
```bash
git clone https://github.com/psummo/drive-tube.git
cd drive-tube
python3 yt_mjpeg.py
```
Il server ascolta sulla porta **8090**: apri `http://IP-DELLA-MACCHINA:8090/`. Su AWS apri la porta 8090 nel security group dell'istanza. Per usare un'altra porta: `YT_PORT=9000 python3 yt_mjpeg.py`.

## Avvio automatico (systemd)
Sostituisci `ec2-user` con il tuo utente (su Ubuntu è `ubuntu`).
```bash
sudo tee /etc/systemd/system/drive-tube.service >/dev/null <<'UNIT'
[Unit]
Description=drive-tube
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/drive-tube
ExecStartPre=-/usr/bin/git -C /home/ec2-user/drive-tube pull --ff-only
ExecStart=/usr/bin/python3 yt_mjpeg.py
Restart=always
StandardOutput=append:/home/ec2-user/yt.log
StandardError=append:/home/ec2-user/yt.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now drive-tube
```
La riga `ExecStartPre` scarica l'ultima versione a ogni avvio. Il log finisce in `~/yt.log` ed è visibile anche dal pannello dell'app.

## Impostazioni
Dal pannello (ingranaggio in alto a sinistra) si scelgono risoluzione, fluidità, qualità video, qualità audio e buffer audio, con valori predefiniti. **Salva e applica** le memorizza nei **cookie del browser**: non esiste nessun file di configurazione sul server. Senza cookie si usano i default scritti nel codice (`SETTINGS` in `yt_mjpeg.py`). Anche le ricerche recenti stanno in un cookie.

## Cookie di YouTube (facoltativo)
Se YouTube chiede di verificare che non sei un robot, esporta i cookie di youtube.com in formato Netscape e caricali dalla pagina `/cookies`. Vengono salvati in `~/cookies.txt` sul server.

## Aggiornamenti
- **Dall'app**: nel pannello, riga *Aggiornamenti*. **Controlla** confronta la versione con quella su `main`; **Installa** scarica il file, ne verifica la sintassi, salva il backup `yt_mjpeg.py.bak` e riavvia il server.
- **A mano**: `cd drive-tube && git pull && sudo systemctl restart drive-tube`. Se hai installato dall'app, prima `git checkout yt_mjpeg.py`.

## Rilasci
Si pubblicano con un tag `vX.Y`: GitHub Actions crea la release con `yt_mjpeg.py` allegato.
