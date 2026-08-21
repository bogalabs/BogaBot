# Deploy — BogaBot en una VPS (Vultr u otra)

Guía de setup **una sola vez** en el server. Después de esto, cada push a
`main` que pase los tests se despliega solo (ver `.github/workflows/deploy.yml`).

## 1. Preparar la VPS (Ubuntu 22.04+)

Conectate por SSH como root la primera vez y creá un usuario dedicado para
el bot (no correr nada como root):

```bash
adduser bogabot
usermod -aG sudo bogabot   # opcional, solo si vas a necesitar sudo para otras cosas
su - bogabot
```

## 2. Clonar el repo e instalar

```bash
cd ~
git clone https://github.com/<tu-usuario>/BogaBot.git
cd BogaBot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Cargar el `.env`

El `.env` **nunca se commitea**. Copiarlo a mano al server (por `scp`) o
crearlo directo con un editor, usando `.env.example` como referencia:

```bash
scp .env bogabot@<IP-DE-LA-VPS>:~/BogaBot/.env
```

## 4. Instalar el servicio systemd

Como usuario con sudo (puede ser el mismo `bogabot` si lo agregaste al
grupo sudo, o root):

```bash
sudo cp ~/BogaBot/deploy/bogabot.service /etc/systemd/system/bogabot.service
sudo systemctl daemon-reload
sudo systemctl enable --now bogabot
sudo systemctl status bogabot   # debería decir "active (running)"
```

Logs en vivo: `sudo journalctl -u bogabot -f`

## 5. Permitir que el deploy automático reinicie el servicio sin contraseña

El workflow de GitHub Actions se conecta como `bogabot` y corre
`sudo systemctl restart bogabot`. Para que no pida password, agregar una
regla de sudoers **acotada solo a ese comando**:

```bash
sudo visudo -f /etc/sudoers.d/bogabot-deploy
```

Contenido del archivo:

```
bogabot ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart bogabot
```

## 6. Generar la SSH key para el deploy automático

Desde tu máquina (o el server), generar un par de claves dedicado al deploy
(sin passphrase, porque la usa un workflow automático):

```bash
ssh-keygen -t ed25519 -f bogabot_deploy_key -N ""
```

- La **pública** (`bogabot_deploy_key.pub`) va en
  `~bogabot/.ssh/authorized_keys` en la VPS.
- La **privada** (`bogabot_deploy_key`) va como secret en GitHub (paso
  siguiente) y después se borra de tu disco.

## 7. Configurar los secrets en GitHub

En el repo → **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret             | Valor                                                    |
|---------------------|-----------------------------------------------------------|
| `VPS_HOST`          | IP pública de la VPS                                      |
| `VPS_USER`          | `bogabot`                                                  |
| `VPS_SSH_KEY`       | Contenido completo de la clave **privada** del paso 6      |
| `VPS_DEPLOY_PATH`   | `/home/bogabot/BogaBot`                                    |

## 8. Listo

A partir de acá, cualquier push (o merge de PR) a `main` que pase
`python -m unittest discover -s tests -v` dispara el deploy automático:
`git pull` + `pip install` + `systemctl restart bogabot` en la VPS.

Para forzar un deploy sin pushear (ej. reintentar), usar el botón **Run
workflow** en la pestaña *Actions* del repo (está habilitado por el
`workflow_dispatch` del workflow).
