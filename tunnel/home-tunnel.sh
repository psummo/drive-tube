#!/bin/sh
# drive-tube: tunnel da casa verso il server.
#
# Gira su un dispositivo sempre acceso a casa (Raspberry, Home Assistant, un Mac
# o un PC Linux) e apre un tunnel SSH verso il server drive-tube. Sul server
# compare un proxy SOCKS5 su 127.0.0.1:1080: le richieste a YouTube che passano
# di li' escono dalla connessione di casa, che per YouTube e' un utente normale,
# e non dall'indirizzo del datacenter. Nessuna porta da aprire sul router: la
# connessione parte da casa.
#
# Prima volta:   SERVER=admin@tuoserver.duckdns.org sh home-tunnel.sh
#                crea una chiave e stampa la riga da aggiungere sul server.
# Poi:           SERVER=admin@tuoserver.duckdns.org sh home-tunnel.sh
#                resta collegato e si ricollega da solo se la linea cade.
#
# Variabili: SERVER (obbligatoria), SSH_PORT (22), PORT (1080),
#            KEY (chiave privata; predefinita accanto a questo script).

DIR=$(cd "$(dirname "$0")" && pwd)
SERVER="${SERVER:-}"
SSH_PORT="${SSH_PORT:-22}"
PORT="${PORT:-1080}"
KEY="${KEY:-$DIR/drivetube_tunnel_key}"

if [ -z "$SERVER" ]; then
  echo "Imposta SERVER, per esempio: SERVER=admin@tuoserver.duckdns.org sh $0" >&2
  exit 1
fi

if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "drive-tube tunnel" -f "$KEY" || exit 1
  chmod 600 "$KEY"
  echo
  echo "Chiave creata. Sul server aggiungi questa riga (tutta su una riga) in ~/.ssh/authorized_keys:"
  echo
  echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",command=\"/usr/sbin/nologin\" $(cat "$KEY.pub")"
  echo
  echo "La chiave puo' solo aprire il tunnel: niente shell, niente altre porte."
  echo "Poi rilancia questo script."
  exit 0
fi

while true; do
  echo "$(date '+%Y-%m-%d %H:%M:%S') apro il tunnel verso $SERVER (SOCKS5 sul server: 127.0.0.1:$PORT)"
  # -R senza destinazione = proxy SOCKS sul server che esce da qui (OpenSSH 7.6+)
  ssh -N -T -p "$SSH_PORT" -i "$KEY" \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
      -o BatchMode=yes -o IdentitiesOnly=yes \
      -R "127.0.0.1:$PORT" "$SERVER"
  echo "$(date '+%Y-%m-%d %H:%M:%S') tunnel chiuso (codice $?), riprovo tra 15 secondi"
  sleep 15
done
