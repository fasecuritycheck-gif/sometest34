'use strict';

const express = require('express');
const app = express();

const {
  TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID,
  FRONTEND_ORIGIN
} = process.env;



const path = require('node:path');

app.get('/', (_req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

app.use('/send-word', (req, res, next) => {
  if (req.headers.origin !== FRONTEND_ORIGIN) {
    return res.status(403).json({ error: 'Origin not allowed.' });
  }

  res.setHeader('Access-Control-Allow-Origin', FRONTEND_ORIGIN);
  res.setHeader('Vary', 'Origin');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

app.use(express.json({ limit: '32kb' }));

// Basic per-instance sending limit.
let nextSendAt = 0;

app.post('/send-word', async (req, res) => {
  const text = typeof req.body?.text === 'string'
    ? req.body.text.trim()
    : '';

  if (!text || text.length > 4000) {
    return res.status(400).json({
      error: 'Enter between 1 and 4000 characters.'
    });
  }

  if (Date.now() < nextSendAt) {
    return res.status(429).json({
      error: 'Please wait a few seconds.'
    });
  }

  nextSendAt = Date.now() + 3000;

  try {
    const response = await fetch(
      `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: TELEGRAM_CHAT_ID,
          text
        }),
        signal: AbortSignal.timeout(10000)
      }
    );

    const result = await response.json();

    if (!response.ok || !result.ok) {
      return res.status(502).json({
        error: 'Telegram rejected the message. Check bot configuration.'
      });
    }

    res.json({ ok: true });
  } catch {
    res.status(502).json({
      error: 'Delivery uncertain. Check Telegram before retrying.'
    });
  }
});

app.use((err, req, res, next) => {
  res.status(err.status === 413 ? 413 : 400).json({
    error: 'Invalid request body.'
  });
});

app.listen(process.env.PORT || 3000, '0.0.0.0');