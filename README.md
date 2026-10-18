# drive-tube
Player YouTube leggero per il browser dell'auto (MJPEG + Web Audio API).

Un unico file Python. `yt-dlp` trova lo stream YouTube e `ffmpeg` lo prepara per il browser, che lo riproduce su canvas e AudioContext, senza tag `<video>` o `<audio>`, con l'audio come orologio principale.

Due formati video, scelti nelle impostazioni:
- **H.264** (predefinito): il video arriva così com'è da YouTube, senza ricodifica, e lo decodifica il browser con il decoder di FFmpeg compilato in WebAssembly, disegnandolo con WebGL. Pesa circa 0,5-2 Mbit/s e il server quasi non lavora. Non richiede HTTPS. Serve un browser con WebAssembly SIMD (Chrome 91 o successivi; il browser Tesla lo ha).
- **MJPEG**: il server trasforma il video in immagini JPEG. Pesa molto di più (decine di Mbit/s) ma funziona ovunque. Si usa da solo quando H.264 non è disponibile per un video o il browser non ha WebAssembly SIMD.

L'audio, in entrambi i casi, è MP3 decodificato dal browser.

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

## Accesso, sessioni e pagina admin
Si entra solo con un **codice di accesso**: senza codice non si vede nessuna pagina. I codici si creano, disattivano ed eliminano dalla pagina **`/admin`**, che serve solo al proprietario.

La password admin si mette nel servizio sull'EC2 (non sta nel codice né su GitHub):
```bash
sudo systemctl edit drive-tube
```
e nell'editor scrivi:
```
[Service]
Environment=YT_ADMIN=una-password-lunga
```
poi `sudo systemctl restart drive-tube`. Apri `/admin`, entra con la password e crea un codice per te e uno per ogni amico. Senza `YT_ADMIN` la pagina admin resta spenta.

- Ogni codice ha la sua sessione (video, impostazioni e ffmpeg separati) e si usa da **un solo schermo alla volta**: se lo apri su un altro schermo, il primo si ferma e mostra "Guarda qui" per riprenderlo. Per due schermi con video diversi servono due codici. Al massimo 10 sessioni in tutto.
- Dalla pagina admin vedi le sessioni attive e puoi chiuderle, controlli e installi gli aggiornamenti, riavvii il server, carichi i cookie di YouTube e leggi il log. Nel player non ci sono più aggiornamenti né log.
- **yt-dlp** si aggiorna dalla pagina admin (sezione *Server*): mostra la versione in uso e l'ultima uscita, e il pulsante scarica la versione stabile in `~/.local/bin/yt-dlp`, che da quel momento ha la precedenza su quella di sistema. Nella stessa riga trovi le versioni di ffmpeg e del runtime JavaScript (node o deno).
- Iscrizioni, "Guarda più tardi" e "Mi piace" dell'account YouTube collegato compaiono nella home solo all'admin.
- Disattivare o eliminare un codice chiude subito le sessioni di chi lo usava.
- Dopo 8 tentativi sbagliati dallo stesso indirizzo in 10 minuti l'accesso si blocca per qualche minuto.
- I codici stanno in `~/.config/drive-tube/access.json`, leggibile solo dal tuo utente.

In http codici e password viaggiano in chiaro. Se condividi il server con altri, usa la porta HTTPS (vedi sopra).

## Impostazioni
Dal pannello (ingranaggio in alto a sinistra) si scelgono formato video (H.264 o MJPEG), risoluzione, fluidità e qualità video (queste due solo per MJPEG), qualità audio e buffer audio, con valori predefiniti. In H.264 la risoluzione arriva al massimo a 720p. **Salva e applica** le memorizza nei **cookie del browser**: non esiste nessun file di configurazione sul server. Senza cookie si usano i default scritti nel codice (`SETTINGS` in `yt_mjpeg.py`). Anche le ricerche recenti stanno in un cookie.

## Cookie di YouTube (facoltativo)
Se YouTube chiede di verificare che non sei un robot, esporta i cookie di youtube.com in formato Netscape e caricali dalla pagina admin (sezione *Cookie YouTube*). Vengono salvati in `~/cookies.txt` sul server.

## Aggiornamenti
- **Dalla pagina admin**: sezione *Server*. **Controlla aggiornamenti** confronta la versione con quella su `main`; **Installa** scarica il file, ne verifica la sintassi, salva il backup `yt_mjpeg.py.bak` e riavvia il server.
- **A mano**: `cd drive-tube && git pull`, poi riavvia il servizio. Se avevi installato dall'app, prima `git checkout yt_mjpeg.py`.

## Rilasci
Si pubblicano con un tag `vX.Y`: GitHub Actions crea la release con `yt_mjpeg.py` allegato.

## Pagine di prova
- `/test`: quali funzioni offre il browser (WebCodecs, WebAssembly, WebGL, formati immagine).
- `/wasmtest`: scarica qualche secondo del video aperto in H.264, misura quanti fps il browser riesce a decodificare e lo riproduce in tempo reale.

## Decoder WebAssembly
Il decoder H.264 è incorporato in `yt_mjpeg.py`. Il sorgente del wrapper è in `wasm/h264dec.c`; `wasm/build.sh` lo ricompila (servono solo clang/lld, cmake, ninja e git) e lo reincorpora nel file.
