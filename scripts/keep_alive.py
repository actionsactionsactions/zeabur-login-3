"""
Zeabur Keep Alive Script
使用 Playwright 模拟浏览器登录，保持账户活跃
通过 Cookie 登录（带重试），登录成功后自动更新 Cookie
支持 Telegram 通知和截图
"""

import os
import sys
import time
import base64
from datetime import datetime

import requests
from nacl import encoding, public
from playwright.sync_api import sync_playwright, BrowserContext, Page

ZEABUR_DASHBOARD_URL = 'https://zeabur.com/projects'
SCREENSHOT_PATH = '/tmp/zeabur_dashboard.png'


# ==================== Telegram 通知 ====================

def send_telegram_message(bot_token: str, chat_id: str, message: str) -> bool:
    """发送 Telegram 文本消息"""
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    try:
        response = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
        }, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f'Telegram 消息发送失败: {e}')
        return False


def send_telegram_photo(bot_token: str, chat_id: str, photo_path: str, caption: str = '') -> bool:
    """发送 Telegram 图片"""
    url = f'https://api.telegram.org/bot{bot_token}/sendPhoto'
    try:
        with open(photo_path, 'rb') as photo:
            response = requests.post(url, data={'chat_id': chat_id, 'caption': caption}, files={'photo': photo}, timeout=60)
            response.raise_for_status()
        return True
    except Exception as e:
        print(f'Telegram 图片发送失败: {e}')
        return False


# ==================== GitHub Secret 更新 ====================

def update_github_secret(token: str, owner: str, repo: str, secret_name: str, secret_value: str):
    """更新 GitHub Repository Secret"""
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    
    # 获取仓库公钥
    key_url = f'https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key'
    key_response = requests.get(key_url, headers=headers, timeout=30)
    key_response.raise_for_status()
    key_data = key_response.json()
    
    # 加密
    public_key_bytes = base64.b64decode(key_data['key'])
    sealed_box = public.SealedBox(public.PublicKey(public_key_bytes))
    encrypted = sealed_box.encrypt(secret_value.encode('utf-8'))
    encrypted_value = base64.b64encode(encrypted).decode('utf-8')
    
    # 更新
    update_url = f'https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}'
    requests.put(update_url, headers=headers, json={
        'encrypted_value': encrypted_value,
        'key_id': key_data['key_id'],
    }, timeout=30).raise_for_status()


# ==================== Cookie 处理 ====================

def parse_cookies(cookie_string: str) -> list:
    """解析 Cookie 字符串为 Playwright 格式"""
    cookies = []
    for cookie in cookie_string.split(';'):
        parts = cookie.strip().split('=', 1)
        if len(parts) == 2:
            cookies.append({
                'name': parts[0].strip(),
                'value': parts[1].strip(),
                'domain': '.zeabur.com',
                'path': '/',
            })
    return cookies


def format_cookies(cookies: list) -> str:
    """格式化 Cookies 为字符串"""
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies if 'zeabur.com' in c.get('domain', ''))


# ==================== 登录 ====================

def login_with_cookie(context: BrowserContext, cookie_string: str, max_retries: int = 2) -> tuple[Page, bool]:
    """使用 Cookie 登录（带重试）"""
    print('🍪 尝试 Cookie 登录...')
    context.add_cookies(parse_cookies(cookie_string))
    
    for attempt in range(max_retries + 1):
        page = context.new_page()
        try:
            page.goto(ZEABUR_DASHBOARD_URL, wait_until='networkidle')
            page.wait_for_timeout(3000)
            
            if '/login' not in page.url:
                print(f'✅ Cookie 登录成功 (第 {attempt + 1} 次尝试)')
                return page, True
            else:
                print(f'⚠️ Cookie 第 {attempt + 1} 次尝试失败，页面跳转到登录页')
                page.close()
        except Exception as e:
            print(f'⚠️ Cookie 第 {attempt + 1} 次尝试异常: {e}')
            page.close()
        
        if attempt < max_retries:
            wait = 5 * (attempt + 1)
            print(f'⏳ 等待 {wait} 秒后重试...')
            time.sleep(wait)
    
    print('❌ Cookie 已过期')
    return context.new_page(), False


# ==================== 主逻辑 ====================

def main():
    cookie_string = os.environ.get('ZEABUR_COOKIE')
    repo_token = os.environ.get('REPO_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    tg_bot_token = os.environ.get('TG_BOT_TOKEN')
    tg_chat_id = os.environ.get('TG_CHAT_ID')

    if not cookie_string:
        print('❌ 错误: ZEABUR_COOKIE 未设置')
        sys.exit(1)

    print('🚀 启动浏览器...')
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        
        try:
            page, login_success = login_with_cookie(context, cookie_string)
            
            if not login_success:
                error_msg = '❌ Cookie 登录失败\n💡 请更新 ZEABUR_COOKIE'
                print(error_msg)
                if tg_bot_token and tg_chat_id:
                    send_telegram_message(tg_bot_token, tg_chat_id, error_msg)
                sys.exit(1)
            
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f'✅ 登录成功！\n⏰ 执行时间: {now}')
            
            # 截图
            page.screenshot(path=SCREENSHOT_PATH, full_page=False)
            print('📸 截图已保存')
            
            # 构建日志
            logs = [f'✅ 已访问: 控制台 ({ZEABUR_DASHBOARD_URL})']
            
            # 自动更新 Cookie
            new_cookie_string = format_cookies(context.cookies())
            if repo_token and repo and new_cookie_string:
                print('🔄 正在更新 Cookie...')
                owner, repo_name = repo.split('/')
                update_github_secret(repo_token, owner, repo_name, 'ZEABUR_COOKIE', new_cookie_string)
                print('✅ GitHub Secret ZEABUR_COOKIE 已更新')
                logs.append('✅ 已自动更新 ZEABUR_COOKIE')
            
            # Telegram 通知
            if tg_bot_token and tg_chat_id:
                print('📤 正在发送 Telegram 通知...')
                message = f'''🟢 <b>Zeabur 自动登录</b>

状态: ✅ 成功
时间: {now}

<b>日志:</b>
''' + '\n'.join(logs)
                
                msg_sent = send_telegram_message(tg_bot_token, tg_chat_id, message)
                photo_sent = send_telegram_photo(tg_bot_token, tg_chat_id, SCREENSHOT_PATH, caption='Zeabur 控制台截图')
                if msg_sent and photo_sent:
                    print('✅ Telegram 通知已发送')
                else:
                    print('⚠️ Telegram 通知部分失败')
            else:
                print('⚠️ TG_BOT_TOKEN 或 TG_CHAT_ID 未设置，跳过 Telegram 通知')
        
        except Exception as e:
            error_msg = f'❌ 执行失败: {str(e)}'
            print(error_msg)
            if tg_bot_token and tg_chat_id:
                send_telegram_message(tg_bot_token, tg_chat_id, error_msg)
            sys.exit(1)
        
        finally:
            browser.close()


if __name__ == '__main__':
    main()
