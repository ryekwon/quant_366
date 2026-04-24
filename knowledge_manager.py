
import os
import sys
import time
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ================= 配置 =================
_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_PATH = os.path.join(_DIR, ".agents", "skills", "evolution-log", "SKILL.md")
LLM_API_URL = os.getenv("LLM_API_URL")
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 忽略列表
IGNORE_DIRS = {".venv", ".git", ".state", "__pycache__", "logs"}
EXTENSIONS = {".py", ".yaml", ".json"}

def get_changed_files(hours=24):
    """获取过去 X 小时内修改过的文件"""
    changed = []
    threshold = datetime.now() - timedelta(hours=hours)
    
    for root, dirs, files in os.walk(_DIR):
        # 过滤目录
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        
        for file in files:
            if any(file.endswith(ext) for ext in EXTENSIONS):
                fpath = os.path.join(root, file)
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                    if mtime > threshold:
                        changed.append(fpath)
                except:
                    continue
    return changed

def summarize_changes(files):
    """利用 LLM 总结变更摘要"""
    if not files:
        return "今日无代码变更。"

    file_summaries = []
    for f in files:
        rel_path = os.path.relpath(f, _DIR)
        # 获取最后 100 行作为代表 (简化版)
        try:
            with open(f, 'r', encoding='utf-8') as tf:
                content = tf.read()
                # 进一步缩减片段，防止超长导致 400 错误
                snippet = content[-1000:] if len(content) > 1000 else content
                file_summaries.append(f"### 文件: {rel_path}\n内容片段:\n```python\n{snippet}\n```")
        except:
            continue

    context_str = "\n\n".join(file_summaries)
    prompt = f"""
你是一位资深量化架构师。以下是今日修改过的代码片段。
请根据这些片段，提炼并总结出今日的核心变更点、重构逻辑或新增的功能细节。
要求：
1. 使用 Markdown 无序列表格式。
2. 语言简练（不超过 5 条）。
3. 标注出受影响的关键模块（如 T0 Executor, StatArb 等）。

# 修改文件原始上下文：
{context_str}
"""

    try:
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        resp = requests.post(
            LLM_API_URL,
            headers=headers,
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=180,
            verify=False
        )
        resp.raise_for_status()
        data = resp.json()
        if 'choices' in data:
            return data['choices'][0]['message']['content']
        else:
            return f"总结失败: 响应内容不符合预期。{json.dumps(data)}"
    except Exception as e:
        return f"总结失败: {str(e)}"

def update_skill(summary):
    """将总结追加到 SKILL.md"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 清理摘要中的多余空行
    summary = summary.strip()
    new_entry = f"\n\n## 📅 {today} (Automated Sync)\n{summary}\n"
    
    try:
        # 写入前先读取，避免重复写入同一天的内容
        content = ""
        if os.path.exists(SKILL_PATH):
            with open(SKILL_PATH, 'r', encoding='utf-8') as f:
                content = f.read()
        
        if f"## 📅 {today} (Automated Sync)" in content:
            print(f"⏩ 今日 ({today}) 的同步已存在，跳过。")
            return

        with open(SKILL_PATH, 'a', encoding='utf-8') as f:
            f.write(new_entry)
        print(f"✅ 已更新 Skill Log: {today}")
    except Exception as e:
        print(f"❌ 写入 Skill 失败: {e}")

def run_sync():
    print("🧠 启动每日知识同步引擎...")
    changed_files = get_changed_files(24)
    print(f"📂 发现 {len(changed_files)} 个变更文件。")
    
    if not changed_files:
        print("⏭️ 今日无变更，跳过更新。")
        return
        
    summary = summarize_changes(changed_files)
    if "总结失败" not in summary:
        update_skill(summary)
    else:
        print(f"🚨 知识总结失败，不写入日志: {summary}")

if __name__ == "__main__":
    run_sync()
