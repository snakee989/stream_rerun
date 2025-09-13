🚀 Quick Start
Option 1: Run with Docker (Recommended)
Create videos directory mkdir videos

Run the container docker run -d --name rerun-streaming -p 5000:5000 -v $(pwd)/videos:/app/videos osamaalrogi/rerun_stream:stable

Access the web interface: http://localhost:5000⁠

✨ Features
🔄 24/7 Automated Streaming - Continuous streaming with intelligent restarts
🎥 Multiple Input Sources - Local files + SRT/RTMP streams
⚡ Hardware Acceleration - NVIDIA NVENC, Intel VAAPI, CPU encoding
🎛️ Web Control Panel - Modern, responsive interface
📊 Real-time Monitoring - Live logs and performance metrics
🔒 Production Ready - Comprehensive error handling & security
🎯 Use Cases
Scenario	Description
24/7 Twitch/YouTube	Stream video files continuously to platforms
Stream Relay	Forward SRT/RTMP streams to multiple destinations
Content Loop	Repeat promotional videos or scheduled content
Backup Streaming	Automatic failover for live streams
⚙️ Configuration
Environment Variables
Variable	Default	Description
DEBUG	false	Enable debug logging
MAX_LOG_LINES	500	Log entries to retain
VIDEO_FOLDER	/app/videos	Video files directory
MAX_RESTARTS	10	Maximum restart attempts
Volume Mounts
-v /path/to/videos:/app/videos # Your video files

🎮 Supported Platforms
Twitch - rtmp://live.twitch.tv/app/YOUR_KEY
YouTube - rtmp://a.rtmp.youtube.com/live2/YOUR_KEY
Facebook - rtmps://live-api-s.facebook.com:443/rtmp/YOUR_KEY
Custom RTMP/SRT - Any RTMP or SRT destination
🖥️ Hardware Requirements
Minimum
CPU: 2 cores
RAM: 2GB
Storage: 10GB
Recommended (1080p)
CPU: 4+ cores (or GPU)
RAM: 4GB+
GPU: NVIDIA GTX 10xx+ or Intel iGPU
Storage: SSD preferred
📱 Web Interface
Stream Configuration - Easy setup for any platform
Real-time Logs - Monitor stream health
Hardware Detection - Automatic encoder selection
Status Monitoring - Uptime, restarts, and metrics
