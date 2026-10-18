const express = require('express');
const speakeasy = require('speakeasy');
const QRCode = require('qrcode');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

// טעינת מפתחות וסודות קיימים מהמערכת
const STATE_FILE = path.join(__dirname, 'state.json');
const LOG_FILE = path.join(__dirname, 'auth-logs.json');

// פונקציות עזר לניהול מפתחות 2FA
const generateSecret = () => speakeasy.generateSecret({ name: 'SovereignBeacon' });
const verifyToken = (secret, token) => speakeasy.totp.verify({ secret, token, window: 2 });

// טעינת נתונים קיימים או יצירת חדשים
const loadState = () => {
  try {
    if (fs.existsSync(STATE_FILE)) {
      return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    }
  } catch (e) {
    console.error('Error loading state:', e);
  }
  return { users: {}, authLogs: [] };
};

const saveState = (state) => {
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
  } catch (e) {
    console.error('Error saving state:', e);
  }
};

const logAuthAttempt = (username, success, method, details = {}) => {
  const logEntry = {
    timestamp: new Date().toISOString(),
    username,
    success,
    method,
    details,
    ip: req.ip,
    userAgent: req.get('User-Agent')
  };
  
  const state = loadState();
  state.authLogs.push(logEntry);
  
  // שמירת רק 100 רשומות אחרונות
  if (state.authLogs.length > 100) {
    state.authLogs = state.authLogs.slice(-100);
  }
  
  saveState(state);
};

// מנגנון אימות בסיסי עם אסימות גישה
const authenticateWithToken = (req, res, next) => {
  const token = req.headers['authorization']?.split(' ')[1];
  
  if (!token) {
    logAuthAttempt('anonymous', false, 'token', { error: 'No token provided' });
    return res.status(401).json({ error: 'Authentication required' });
  }
  
  // בדיקת אסימון גישה קיימת
  const state = loadState();
  const user = Object.values(state.users).find(u => u.accessToken === token);
  
  if (!user) {
    logAuthAttempt('anonymous', false, 'token', { error: 'Invalid token' });
    return res.status(403).json({ error: 'Invalid access token' });
  }
  
  req.user = user;
  next();
};

// מנגנון אימות דו-גורמי
const authenticateWith2FA = (req, res, next) => {
  const { otp } = req.body;
  
  if (!otp) {
    logAuthAttempt(req.user.username, false, '2fa', { error: 'No OTP provided' });
    return res.status(401).json({ error: 'Two-factor authentication required' });
  }
  
  const isValid = verifyToken(req.user.secret2FA, otp);
  
  if (!isValid) {
    logAuthAttempt(req.user.username, false, '2fa', { error: 'Invalid OTP' });
    return res.status(403).json({ error: 'Invalid two-factor authentication' });
  }
  
  logAuthAttempt(req.user.username, true, '2fa');
  next();
};

// ניהול הגדרות 2FA למשתמש
app.post('/api/2fa/setup', authenticateWithToken, (req, res) => {
  const user = req.user;
  const secret = generateSecret();
  
  // יצירת QR Code לסריקה
  QRCode.toDataURL(secret.base32, (err, qrCodeUrl) => {
    if (err) {
      return res.status(500).json({ error: 'Failed to generate QR code' });
    }
    
    // שמירת הסוד למשתמש
    user.secret2FA = secret.base32;
    user.twoFactorEnabled = false; // עד אימות הסריקה
    
    const state = loadState();
    state.users[user.username] = user;
    saveState(state);
    
    res.json({ 
      secret: secret.base32,
      qrCodeUrl,
      backupCodes: generateBackupCodes()
    });
  });
});

app.post('/api/2fa/verify', authenticateWithToken, (req, res) => {
  const user = req.user;
  const { token } = req.body;
  
  if (!user.secret2FA) {
    return res.status(400).json({ error: '2FA not set up' });
  }
  
  const isValid = verifyToken(user.secret2FA, token);
  
  if (isValid) {
    user.twoFactorEnabled = true;
    
    const state = loadState();
    state.users[user.username] = user;
    saveState(state);
    
    logAuthAttempt(user.username, true, '2fa-setup');
    res.json({ success: true });
  } else {
    logAuthAttempt(user.username, false, '2fa-setup', { error: 'Invalid token' });
    res.status(400).json({ error: 'Invalid token' });
  }
});

app.post('/api/2fa/disable', authenticateWithToken, authenticateWith2FA, (req, res) => {
  const user = req.user;
  
  user.secret2FA = null;
  user.twoFactorEnabled = false;
  
  const state = loadState();
  state.users[user.username] = user;
  saveState(state);
  
  logAuthAttempt(user.username, true, '2fa-disable');
  res.json({ success: true });
});

// נקודות API מוגנות עם 2FA
app.get('/api/protected-data', authenticateWithToken, authenticateWith2FA, (req, res) => {
  // דוגמה לנתונים מוגנים
  const protectedData = {
    memoryLayers: '[REDACTED — P0-1: restoration/custody detail removed from public surface, master-plan T6, session 8fa4709b]',
    awakening: '[REDACTED — P0-1: restoration/custody detail removed from public surface, master-plan T6, session 8fa4709b]',
    doctrine: '[REDACTED — P0-1: restoration/custody detail removed from public surface, master-plan T6, session 8fa4709b]'
  };
  
  res.json(protectedData);
});

// ניהול לוגים
app.get('/api/auth-logs', authenticateWithToken, authenticateWith2FA, (req, res) => {
  const state = loadState();
  res.json({ logs: state.authLogs });
});

// עזר ליצירת קודי גיבוי
const generateBackupCodes = () => {
  const codes = [];
  for (let i = 0; i < 10; i++) {
    codes.push(crypto.randomBytes(4).toString('hex').toUpperCase());
  }
  return codes;
};

// התחלת השרת
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`SovereignBeacon 2FA API server running on port ${PORT}`);
});