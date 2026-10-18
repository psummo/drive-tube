# drive-tube
Player YouTube leggero per il browser dell'auto (MJPEG + Web Audio API).

Un unico file Python: `yt-dlp` e `ffmpeg` trasformano lo stream YouTube in MJPEG (video) e MP3 (audio), il browser li riproduce su canvas e AudioContext con l'audio come orologio principale.

## Requisiti
Una macchina Linux con:
- `python3` e `git`
- `ffmpeg` con supporto `libmp3lame` (la maggior parte delle build lo include)
- `yt-dlp`

## Installazione
Installa le dipendenze con il gestore pacchetti della tua distribuzione:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 git ffmpeg curl

# Fedora / RHEL e derivate
sudo dnf install -y python3 git ffmpeg curl
```
Se `ffmpeg` non è nei repository della tua distribuzione, scarica una build statica per Linux e copia il binario in `/usr/local/bin`.

Installa `yt-dlp` (binario autonomo, non richiede altre librerie Python):
```bash
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```
Le versioni recenti di yt-dlp possono richiedere un runtime JavaScript per YouTube: se nel log compaiono avvisi, installa anche `nodejs` (o Deno).

Scarica e avvia:
```bash
git clone https://github.com/psummo/drive-tube.git
cd drive-tube
python3 yt_mjpeg.py
```
Il server ascolta sulla porta **8090**: apri `http://INDIRIZZO-DELLA-MACCHINA:8090/`. Se c'è un firewall, apri la porta 8090 in ingresso. Per usare un'altra porta: `YT_PORT=9000 python3 yt_mjpeg.py`.

## Avvio automatico (systemd)
Dalla cartella `drive-tube`, questi comandi creano il servizio con il tuo utente e il percorso corrente:
```bash
sudo tee /etc/systemd/system/drive-tube.service >/dev/null <<UNIT
[Unit]
Description=drive-tube
After=network.target

[Service]
User=$(whoami)
WorkingDirectory=$PWD
ExecStartPre=-$(command -v git) -C $PWD pull --ff-only
ExecStart=$(command -v python3) yt_mjpeg.py
Restart=always
StandardOutput=append:$HOME/yt.log
StandardError=append:$HOME/yt.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now drive-tube
```
La riga `ExecStartPre` scarica l'ultima versione a ogni avvio del servizio. Il log finisce in `~/yt.log` ed è visibile anche dal pannello dell'app. Per fermare o riavviare: `sudo systemctl stop|restart drive-tube`.

## HTTPS (facoltativo)
L'app funziona anche solo in http. L'HTTPS serve alle funzioni del browser riservate alle pagine sicure (per esempio la decodifica video WebCodecs). Il server lo attiva da solo, su una porta separata (**8443**), se trova un certificato in `~/yt-cert/fullchain.pem` e `~/yt-cert/privkey.pem`. La porta http 8090 resta attiva.

Serve un nome di dominio che punta alla macchina (va bene uno gratuito di [DuckDNS](https://www.duckdns.org)). Il certificato gratuito di Let's Encrypt si ottiene con certbot:

1. Apri in ingresso le porte **80** (serve a Let's Encrypt per la verifica) e **8443**. Su AWS EC2: *Security group → Inbound rules*.
2. Installa certbot:
   ```bash
   sudo apt install -y certbot   # Debian / Ubuntu
   sudo dnf install -y certbot   # Fedora / RHEL / Amazon Linux
   ```
3. Dalla cartella `drive-tube`, sostituisci il dominio e lancia:
   ```bash
   DOM=tuonome.duckdns.org
   sudo certbot certonly --standalone -d $DOM --agree-tos --register-unsafely-without-email --non-interactive \
     --deploy-hook "install -D -m 600 -o $(whoami) /etc/letsencrypt/live/$DOM/fullchain.pem $HOME/yt-cert/fullchain.pem && install -D -m 600 -o $(whoami) /etc/letsencrypt/live/$DOM/privkey.pem $HOME/yt-cert/privkey.pem && systemctl restart drive-tube"
   ```
   Il deploy hook copia il certificato dove il server lo legge e riavvia il servizio. Lo rifà da solo a ogni rinnovo automatico (ogni circa 60 giorni), purché la porta 80 resti aperta.
4. Apri `https://tuonome.duckdns.org:8443/`. Nel log compare `https://0.0.0.0:8443/`. Se invece compare `https spento`, il certificato non è stato trovato.

Porta e percorsi si cambiano con `YT_HTTPS_PORT`, `YT_CERT` e `YT_KEY`.

## Impostazioni
Dal pannello (ingranaggio in alto a sinistra) si scelgono risoluzione, fluidità, qualità video, qualità audio e buffer audio, con valori predefiniti. **Salva e applica** le memorizza nei **cookie del browser**: non esiste nessun file di configurazione sul server. Senza cookie si usano i default scritti nel codice (`SETTINGS` in `yt_mjpeg.py`). Anche le ricerche recenti stanno in un cookie.

## Cookie di YouTube (facoltativo)
Se YouTube chiede di verificare che non sei un robot, esporta i cookie di youtube.com in formato Netscape e caricali dalla pagina `/cookies`. Vengono salvati in `~/cookies.txt` sul server.

## Aggiornamenti
- **Dall'app**: nel pannello, riga *Aggiornamenti*. **Controlla** confronta la versione con quella su `main`; **Installa** scarica il file, ne verifica la sintassi, salva il backup `yt_mjpeg.py.bak` e riavvia il server.
- **A mano**: `cd drive-tube && git pull`, poi riavvia il servizio. Se avevi installato dall'app, prima `git checkout yt_mjpeg.py`.

## Rilasci
Si pubblicano con un tag `vX.Y`: GitHub Actions crea la release con `yt_mjpeg.py` allegato.
