# Montar a TV do NOC — passo a passo

Este é o lado **fora do navegador** do painel (P9). Não é código: é a máquina da
sala. Uma TV de NOC fica meses ligada, ninguém tem teclado à mão quando ela cai,
e é por isso que cada passo aqui existe.

O lado de dentro já está resolvido: a página se recarrega sozinha a cada 15 min
(JS que engasga volta ao normal) e a credencial de pareamento não expira, então
a TV religa e volta ao painel sem ninguém.

---

## 1. Parear

1. na TV, abra `https://velus.seujaime.com/paineis/noc/`;
2. ela mostra um QR e um código de seis letras;
3. escaneie com o celular (ou abra `velus.seujaime.com/paineis/aprovar/` e digite
   o código), **faça login** e aprove, dando um nome à TV ("TV da bancada");
4. a TV entra no painel sozinha em alguns segundos.

O código vale 5 minutos e serve uma vez só. Se expirar, a própria tela da TV se
renova e mostra outro.

**Quem aprova precisa ter acesso à aba de Massivas** — a TV não pode ganhar um
acesso que quem a aprovou não tem.

Para derrubar uma TV: *Configurações → TVs pareadas → revogar*. Ela volta para a
tela de pareamento no próximo carregamento.

---

## 2. Kiosk no Chrome/Chromium (Linux)

```bash
chromium \
  --kiosk \
  --app=https://velus.seujaime.com/paineis/noc/ \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --incognito=false
```

`--noerrdialogs` e `--disable-session-crashed-bubble` existem pelo mesmo motivo:
depois de uma queda de energia, o Chrome abre um balão "o navegador não foi
encerrado corretamente" **por cima do painel**, e ele fica lá até alguém clicar.
Numa sala sem teclado, isso é a TV inutilizada.

**Não use `--incognito`.** A credencial da TV vive num cookie e o registro de
"esta massiva eu já anunciei" vive no `localStorage`: em aba anônima, a TV pede
pareamento a cada reinício e repete o takeover do mesmo evento.

---

## 3. Autostart

Com systemd, na sessão do usuário da TV:

```ini
# ~/.config/systemd/user/painel-noc.service
[Unit]
Description=Painel NOC na TV
After=graphical-session.target

[Service]
ExecStart=/usr/bin/chromium --kiosk --app=https://velus.seujaime.com/paineis/noc/ --noerrdialogs --disable-session-crashed-bubble
Restart=always
RestartSec=10

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user enable --now painel-noc.service
loginctl enable-linger "$USER"   # a sessão sobe sem alguém logar na máquina
```

`Restart=always` é o watchdog mais barato que existe: se o Chrome morrer, ele
volta em 10 s.

---

## 4. Tela sempre acesa

```bash
xset s off        # sem protetor de tela
xset -dpms        # sem desligar a tela
xset s noblank
```

Em Wayland, o equivalente depende do compositor — no GNOME, desligue
*"Blank screen"* e a suspensão automática nas configurações de energia.

---

## 5. O que conferir depois de montar

- [ ] a TV voltou sozinha ao painel depois de desligar e religar na tomada;
- [ ] a idade do dado, no canto direito, **anda** (sobe de segundo em segundo);
- [ ] puxe o cabo de rede por um minuto: a faixa âmbar de *dado velho* tem que
      aparecer. É o teste que prova que a tela não vai mentir no dia em que a
      coleta cair;
- [ ] a rotação continua trocando de slide com a rede fora (ela é independente
      do dado, de propósito);
- [ ] *Configurações → TVs pareadas* mostra a TV com "vista há poucos segundos".

O terceiro item é o mais importante de todos. Um painel que congela com números
antigos e cara de vivo é pior que um painel apagado.
