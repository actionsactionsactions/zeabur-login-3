这是一个支持**多账户登录**的修改版本。

### 主要修改点：
1.  **引入配置文件机制**：通过一个新的环境变量 `ACCOUNTS_CONFIG` (JSON 格式) 来定义多个账户的信息。
2.  **隔离运行环境**：每个账户使用独立的 `BrowserContext`，防止 Cookie 串扰。
3.  **动态密钥更新**：脚本会根据配置，更新对应账户在 GitHub Secret 中指定的 Cookie 变量名。
4.  **向后兼容**：如果没有配置 `ACCOUNTS_CONFIG`，脚本会自动回退到原本的单账户模式（读取 `ZEABUR_COOKIE` 等）。

### 1. 配置说明 (在 GitHub Actions Secret 中设置)

你需要设置一个名为 `ACCOUNTS_CONFIG` 的 Secret，内容为 JSON 数组：

```json
[
  {
    "name": "账户A (个人)",
    "cookie_env": "ZEABUR_COOKIE_A",
    "magic_link_env": "ZEABUR_MAGIC_LINK_A"
  },
  {
    "name": "账户B (公司)",
    "cookie_env": "ZEABUR_COOKIE_B",
    "magic_link_env": "ZEABUR_MAGIC_LINK_B"
  }
]
```

同时，你需要在 Secrets 中分别设置好上面引用的变量（如 `ZEABUR_COOKIE_A`, `ZEABUR_MAGIC_LINK_A` 等）。

---

### 2. 修改后的脚本代码

```python
"""
Zeabur Keep Alive Script (Multi-Account Support)
使用 Playwright 模拟浏览器登录，保持多个账户活跃
支持 Magic Link 登录（优先）和 Cookie 登录（备选）
登录成功后发送 Telegram 通知和截图，并自动更新对应账户的 Cookie
"""

import os
import sys
import json
import base64
import time
from datetime import datetime

import requests
from nacl import encoding, public
from playwright.sync_api import sync_playwright, BrowserContext, Page, Browser

ZEABUR_DASHBOARD_URL = 'https://zeabur.com/projects'
# 截图路径将根据账户名动态生成

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
    if not cookie_string:
        return cookies
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


# ==================== 登录方式 ====================

def login_with_magic_link(context: BrowserContext, magic_link: str) -> tuple[Page, bool]:
    """使用 Magic Link 登录"""
    print('🔗 尝试 Magic Link 登录...')
    page = context.new_page()
    page.set_default_timeout(60000)
    
    try:
        page.goto(magic_link, timeout=60000, wait_until='domcontentloaded')
        page.wait_for_timeout(5000)
        
        if '/login' not in page.url:
            print('✅ Magic Link 登录成功')
            page.goto(ZEABUR_DASHBOARD_URL, wait_until='networkidle')
            page.wait_for_timeout(2000)
            return page, True
        else:
            print('❌ Magic Link 已失效或无效')
            return page, False
    except Exception as e:
        print(f'❌ Magic Link 登录失败: {e}')
        return page, False


def login_with_cookie(context: BrowserContext, cookie_string: str) -> tuple[Page, bool]:
    """使用 Cookie 登录"""
    print('🍪 尝试 Cookie 登录...')
    context.add_cookies(parse_cookies(cookie_string))
    page = context.new_page()
    
    try:
        page.goto(ZEABUR_DASHBOARD_URL, wait_until='networkidle')
        page.wait_for_timeout(3000)
        
        # 检查是否仍在登录页或者被重定向回登录页
        if '/login' not in page.url and 'zeabur.com' in page.url:
            print('✅ Cookie 登录成功')
            return page, True
        else:
            print('❌ Cookie 已过期')
            return page, False
    except Exception as e:
        print(f'❌ Cookie 登录失败: {e}')
        return page, False


# ==================== 单个账户处理逻辑 ====================

def process_account(browser: Browser, account_config: dict, global_env: dict) -> bool:
    """
    处理单个账户的登录和保活
    """
    account_name = account_config.get('name', 'Unknown Account')
    cookie_env_name = account_config.get('cookie_env')
    magic_link_env_name = account_config.get('magic_link_env')
    
    # 从环境变量获取实际的值
    cookie_string = os.environ.get(cookie_env_name, '')
    magic_link = os.environ.get(magic_link_env_name, '')
    
    print(f'\n🔹 开始处理账户: [{account_name}]')
    
    # 准备环境参数
    repo_token = global_env.get('REPO_TOKEN')
    repo = global_env.get('GITHUB_REPOSITORY', '')
    tg_bot_token = global_env.get('TG_BOT_TOKEN')
    tg_chat_id = global_env.get('TG_CHAT_ID')
    
    if not magic_link and not cookie_string:
        print(f'⚠️ 账户 [{account_name}] 未配置 Cookie 或 Magic Link，跳过。')
        return False

    context = browser.new_context()
    page = None
    login_success = False
    login_method = None
    screenshot_path = f'/tmp/zeabur_{int(time.time())}_{account_name.replace(" ", "_")}.png'

    try:
        # 1. 优先尝试 Cookie
        if cookie_string:
            page, login_success = login_with_cookie(context, cookie_string)
            if login_success:
                login_method = 'Cookie'
        
        # 2. Cookie 失效时回退到 Magic Link
        if not login_success and magic_link:
            if page:
                page.close()
            page, login_success = login_with_magic_link(context, magic_link)
            if login_success:
                login_method = 'Magic Link'
        
        # 3. 登录失败处理
        if not login_success:
            error_msg = f'❌ 账户 [{account_name}] 所有登录方式均失败\n💡 请检查 {magic_link_env_name}'
            print(error_msg)
            if tg_bot_token and tg_chat_id:
                send_telegram_message(tg_bot_token, tg_chat_id, error_msg)
            context.close()
            return False
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f'✅ 账户 [{account_name}] 登录成功！({login_method})')
        
        # 截图
        try:
            page.screenshot(path=screenshot_path, full_page=False)
            print(f'📸 截图已保存: {screenshot_path}')
        except Exception as e:
            print(f'⚠️ 截图失败: {e}')

        # 构建日志
        logs = [f'✅ 账户: {account_name}', f'✅ 方式: {login_method}']
        
        # 4. 更新 Cookie
        new_cookie_string = format_cookies(context.cookies())
        if repo_token and repo and new_cookie_string and cookie_env_name:
            # 只有当 Cookie 确实改变且与原值不同时才更新 (简单的字符串比较)
            # 注意：Magic Link 登录肯定需要更新 Cookie
            if new_cookie_string != cookie_string or login_method == 'Magic Link':
                print(f'🔄 正在更新 GitHub Secret: {cookie_env_name}...')
                try:
                    owner, repo_name = repo.split('/')
                    update_github_secret(repo_token, owner, repo_name, cookie_env_name, new_cookie_string)
                    print(f'✅ Secret [{cookie_env_name}] 已更新')
                    logs.append(f'✅ Secret {cookie_env_name} 已自动更新')
                except Exception as e:
                    print(f'⚠️ Secret 更新失败: {e}')
                    logs.append(f'⚠️ Secret 更新失败: {e}')
        
        # 5. Telegram 通知
        if tg_bot_token and tg_chat_id:
            print('📤 正在发送 Telegram 通知...')
            message = f'''🟢 <b>Zeabur 保活通知</b>

👤 账户: <b>{account_name}</b>
🛠 状态: ✅ 成功
🔑 方式: {login_method}
⏰ 时间: {now}

<b>详细日志:</b>
''' + '\n'.join(logs)
            
            send_telegram_message(tg_bot_token, tg_chat_id, message)
            send_telegram_photo(tg_bot_token, tg_chat_id, screenshot_path, caption=f'Dashboard: {account_name}')
            print('✅ 通知已发送')
        
        return True

    except Exception as e:
        error_msg = f'❌ 账户 [{account_name}] 执行出错: {str(e)}'
        print(error_msg)
        if tg_bot_token and tg_chat_id:
            send_telegram_message(tg_bot_token, tg_chat_id, error_msg)
        return False
        
    finally:
        if context:
            context.close()
        # 清理截图文件
        if os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
            except:
                pass


# ==================== 主入口 ====================

def main():
    # 获取全局配置
    repo_token = os.environ.get('REPO_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    tg_bot_token = os.environ.get('TG_BOT_TOKEN')
    tg_chat_id = os.environ.get('TG_CHAT_ID')
    
    # 构造全局环境字典传递给处理函数
    global_env = {
        'REPO_TOKEN': repo_token,
        'GITHUB_REPOSITORY': repo,
        'TG_BOT_TOKEN': tg_bot_token,
        'TG_CHAT_ID': tg_chat_id
    }

    accounts = []
    
    # 检查是否有 ACCOUNTS_CONFIG (多账户模式)
    accounts_config_json = os.environ.get('ACCOUNTS_CONFIG')
    
    if accounts_config_json:
        try:
            accounts = json.loads(accounts_config_json)
            print(f'📋 检测到多账户配置，共 {len(accounts)} 个账户')
        except json.JSONDecodeError:
            print('❌ ACCOUNTS_CONFIG JSON 格式错误')
            sys.exit(1)
    else:
        # 兼容旧版单账户模式
        print('ℹ️ 未检测到 ACCOUNTS_CONFIG，尝试单账户兼容模式')
        if os.environ.get('ZEABUR_COOKIE') or os.environ.get('ZEABUR_MAGIC_LINK'):
            accounts.append({
                "name": "Default Account",
                "cookie_env": "ZEABUR_COOKIE",
                "magic_link_env": "ZEABUR_MAGIC_LINK"
            })
        else:
            print('❌ 错误: 未配置任何账户信息 (ACCOUNTS_CONFIG 或 ZEABUR_COOKIE/MAGIC_LINK)')
            sys.exit(1)

    print('🚀 启动浏览器...')
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        success_count = 0
        for account in accounts:
            if process_account(browser, account, global_env):
                success_count += 1
            # 账户之间稍微停顿
            time.sleep(2)
            
        browser.close()
    
    print(f'\n🏁 所有任务执行完毕。成功: {success_count}/{len(accounts)}')
    
    if success_count < len(accounts):
        sys.exit(1) # 如果有失败，标记 Workflow 为失败

if __name__ == '__main__':
    main()
```
