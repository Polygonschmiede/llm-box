# Running it from another machine — SSH, tunnels, Wake-on-LAN

The server is usually not the machine you sit at. This page covers logging in,
reaching the web interfaces safely, and waking the machine over the network.

Throughout, `<server-ip>` is the address of the box and `<user>` your account on it.
`llm url` prints the addresses when run on the server itself.

## SSH

Once, on your workstation:

```bash
ssh-keygen -t ed25519                  # if you do not have a key yet
ssh-copy-id <user>@<server-ip>         # no more password prompts
```

Then give it a short name in `~/.ssh/config`:

```
Host llm-box
    HostName <server-ip>
    User <user>
```

After that:

```bash
ssh llm-box                # log in
ssh llm-box llm ls         # run a single command
ssh llm-box llm status
ssh llm-box llm restart
```

`setup-system.sh` enables the SSH server at boot. Check with
`systemctl is-enabled ssh`.

## Reaching the web interfaces

The services ship bound to `127.0.0.1`, so out of the box they are only reachable on
the machine itself. There are two ways to change that, and the tunnel is the safer
one.

**Port forwarding over SSH** — nothing is exposed to the network, only you get
through:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 -L 8188:127.0.0.1:8188 llm-box
```

Then use `http://localhost:3000` and so on, on your own machine.

**Opening the ports on the LAN** — convenient inside a network you trust:

```bash
sudo env LLM_BIND=0.0.0.0 bash setup-system.sh
```

That also adds firewall rules for your own subnet only. Read
[../SECURITY.md](../SECURITY.md) first: llama-swap and ComfyUI have no
authentication at all.

## Wake-on-LAN

So you do not have to walk over to the machine: start it from sleep or soft-off over
the network.

**1. Enable it in the BIOS/UEFI** (once, at the machine). The option is usually under
`Advanced → APM Configuration` and is called **"Power On By PCI-E/PCI"** or
**"Wake on LAN"** — names vary by vendor. Save and reboot.

**2. Enable it on the server.** `setup-system.sh` does this: it detects the network
adapter from the default route and installs a small service that runs
`ethtool -s <nic> wol g` at every boot. Check it:

```bash
ip -o -4 route show to default | awk '{print $5}'    # your adapter, e.g. eno1
sudo ethtool <nic> | grep Wake-on
# want:  Wake-on: g       (g = magic packet)
```

If it says `Wake-on: d` the feature is off — check the BIOS option and restart the
service: `sudo systemctl restart wol@<nic>`.

**3. Find the MAC address** (that is what you send the packet to):

```bash
cat /sys/class/net/<nic>/address
```

**4. Give the machine a stable address.** In your router, assign it a fixed DHCP
lease so its IP does not move.

**5. Wake it from your workstation.** Install a wake tool once (`apt install
wakeonlan`, or `brew install wakeonlan` on macOS), then:

```bash
wakeonlan <mac-address>
sleep 25
ssh llm-box
```

Worth wrapping in a shell function:

```bash
llmwake() { wakeonlan <mac-address>; echo "waking…"; sleep 25; ssh llm-box; }
```

### Notes

- Wake-on-LAN works over **cable** only, not Wi-Fi.
- From full soft-off (S5) the BIOS option in step 1 is required. From suspend it
  usually works without it.
- The machine has to stay on mains power with the network cable plugged in.
