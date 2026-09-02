# MFD Raymarine simulé — mDNS, découverte 5800, RayDB 23333, RRCE 50000,
# recopie d'écran RTSP 8554 (GStreamer), SFTP 22.
#
# Base Debian (et non Alpine) : c'est le seul écosystème qui *package* le
# « GStreamer RTSP server » — la brique exacte du vrai MFD. L'image est donc
# nettement plus lourde (~700 Mo) ; c'est le coût inhérent de GStreamer.
# Pour une image légère sans vidéo, lancer avec `--no-rtsp` (le port 8554 n'est
# alors pas servi) — mais les paquets GStreamer restent dans l'image.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-gi python3-paramiko python3-cryptography \
        gir1.2-gst-rtsp-server-1.0 \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    && pip3 install --no-cache-dir --break-system-packages "zeroconf>=0.130" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mfdsim/ /app/mfdsim/
COPY video/ /app/video/

# SSH/SFTP servi par le sshd Python (paramiko), exactement comme hors conteneur :
# le port 22 est raciné sur l'arbre UserData du MFD (créé au démarrage) et la clé
# publique enrôlée sur 8182 y ouvre l'accès sans redémarrage. La clé d'hôte RSA
# est persistée sous /etc/ssh/keys : monter un volume dessus fige l'empreinte
# entre recréations du conteneur (sinon les clients hurlent au changement).
ENV MFD_SFTP_ROOT=/data/media/0/UserData
ENV MFD_SSH_HOST_KEY=/etc/ssh/keys/ssh_host_rsa_key

# 22 SFTP · 5800/udp découverte · 5353/udp mDNS · 23333 RayDB · 50000 RRCE · 8554 RTSP · 8182 messages
EXPOSE 22 23333 50000 8554 8182
EXPOSE 5800/udp 5353/udp

# Le multicast (5800 et mDNS) ne traverse pas le NAT bridge de Docker :
# --network host est nécessaire pour que la découverte fonctionne (cf. README).
# Le port 22 étant privilégié, le conteneur tourne en root (défaut) : le sshd
# Python peut l'ouvrir sans réglage.
ENTRYPOINT ["python3", "-m", "mfdsim"]
