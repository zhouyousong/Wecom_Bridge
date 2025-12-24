#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业微信客服独立网站接收服务（V9 - 游标版）
 - 移除 CSV 去重，改用 Cursor 游标机制
 - 自动刷新 access_token
 - 支持图片、文件、视频、位置存储
"""

from flask import Flask, request, make_response
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise import WeChatClient
import xml.etree.ElementTree as ET
import requests
import os
import time
import traceback
import json
import yt_dlp
import re

# ==== 企业微信配置 ====
CORP_ID = "https://work.weixin.qq.com/kf/frame#/config 页面下 企业ID"
CORP_SECRET = "https://work.weixin.qq.com/kf/frame#/config 页面下 Secret""
TOKEN = "https://work.weixin.qq.com/kf/frame#/config 页面下 Token""
ENCODING_AES_KEY = "https://work.weixin.qq.com/kf/frame#/config 页面下 EncodingAESKey""
AGENT_ID = 1000002

# 路径配置
PIC_SAVE_PATH = "你的照片文件夹"
MP3_SAVE_PATH = "你的音频文件夹"
MP4_SAVE_PATH = "你的视频文件夹"
CURSOR_DIR = "存放临时光标的文件夹"  # 新增：专门存放游标文件的文件夹

# ==== 初始化 ====
app = Flask(__name__)
crypto = WeChatCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID)
client = WeChatClient(CORP_ID, CORP_SECRET)

os.makedirs(PIC_SAVE_PATH, exist_ok=True)
os.makedirs(CURSOR_DIR, exist_ok=True)

# ==== token 缓存 ====
token_cache = {"access_token": None, "expire_time": 0}

def get_access_token():
    """获取最新 access_token，如果过期则刷新"""
    now = time.time()
    if token_cache["access_token"] and now < token_cache["expire_time"]:
        return token_cache["access_token"]

    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={CORP_SECRET}"
    resp = requests.get(url).json()
    if resp.get("errcode") != 0:
        raise Exception(f"获取 access_token 失败: {resp}")
    token_cache["access_token"] = resp["access_token"]
    token_cache["expire_time"] = now + resp.get("expires_in", 7200) - 60
    print(f"[Token刷新] {token_cache['access_token']}")
    return token_cache["access_token"]

# ==== 游标管理 (新增) ====
def get_cursor_path(open_kfid):
    """根据客服ID生成唯一的游标文件路径"""
    safe_id = open_kfid.replace("/", "_")
    return os.path.join(CURSOR_DIR, f"{safe_id}.txt")

def load_cursor(open_kfid):
    """读取本地存储的游标"""
    path = get_cursor_path(open_kfid)
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    return ""

def save_cursor(open_kfid, cursor):
    """保存新的游标到本地"""
    if not cursor:
        return
    path = get_cursor_path(open_kfid)
    with open(path, "w") as f:
        f.write(cursor)
    # 调试用，如果不想看保存游标的日志可以注释掉
    # print(f"[游标更新] {cursor[:10]}...") 
Locker = False
@app.route("/wechat", methods=["GET", "POST"])
def wechat():
    global Locker
    try:
        if request.method == "GET":
            msg_signature = request.args.get("msg_signature")
            timestamp = request.args.get("timestamp")
            nonce = request.args.get("nonce")
            echostr = request.args.get("echostr")
            return crypto.check_signature(msg_signature, timestamp, nonce, echostr)

        msg_signature = request.args.get("msg_signature")
        timestamp = request.args.get("timestamp")
        nonce = request.args.get("nonce")
        encrypt_xml = request.data

        xml_content = crypto.decrypt_message(encrypt_xml, msg_signature, timestamp, nonce)
        xml_tree = ET.fromstring(xml_content)
        msg_type = xml_tree.findtext("MsgType")
        event = xml_tree.findtext("Event")
        open_kfid = xml_tree.findtext("OpenKfId")

        print(f"[回调收到] ~~~~~~~~~~~~~~~~~~~~~~~ type={msg_type}, event={event}, kfid={open_kfid}",end = "")

        # 改动：这里不再传递 Token，而是传递 OpenKfId
        # 因为我们要用 Cursor + OpenKfId 来拉取消息，而不是用 Token
        if msg_type == "event" and event == "kf_msg_or_event" and open_kfid:
            if Locker == False:
                print(f"没有锁,可以调用sync_messages")
                sync_messages(open_kfid)
            else:
                print("被锁住了，不调用，退出")
        return make_response("success")

    except Exception as e:
        print("[错误] 处理失败：", e)
        traceback.print_exc()
        return make_response("error", 500)


def sync_messages(open_kfid):
    
    
    """
    使用 游标(cursor) 机制同步消息
    不再使用 event token，而是使用 open_kfid + cursor
    """
    global Locker
    Locker = True
    print("sync_messages 锁住了")
    while True:
    
        try:
            access_token = get_access_token()
            cursor = load_cursor(open_kfid)
            
            url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"
            
            # 构造请求包：如果有游标就带上，没有就只带 kfid
            payload = {"open_kfid": open_kfid}
            if cursor:
                payload["cursor"] = cursor
                
            resp = requests.post(url, json=payload).json()
            
            if resp.get("errcode") != 0:
                print(f"[sync_msg失败] {resp.get('errcode')} {resp.get('errmsg')}")
                return

            msg_list = resp.get("msg_list", [])
            # 如果没有新消息，直接返回
            if not msg_list:
                print("[没有新的消息]，释放锁")
                Locker = False
                return

            print(f"[同步消息] 获取到 {len(msg_list)} 条新消息")

            # 处理消息列表
            for msg in msg_list:
                process_sync_msg(msg)
                time.sleep(1)

            # 只有当所有消息处理完没有报错时，才保存新的游标
            # 这样如果处理中途崩溃，下次重启会重新拉取，保证不丢消息
            next_cursor = resp.get("next_cursor")
            if next_cursor:
                save_cursor(open_kfid, next_cursor)

        except Exception as e:
            print(f"[sync_msg异常] {e}")
            traceback.print_exc()
        
        time.sleep(10)
        print("等待10s看看有没有新的消息")
    
CMD_Pointer = "NA"


def extract_link(text):
    """
    从文本中提取第一个 http 或 https 链接
    """
    # 正则解释：
    # https?  -> 匹配 http 或 https
    # ://     -> 匹配 ://
    # \S+     -> 匹配非空白字符（直到遇到空格、换行或字符串结束）
    pattern = r"(https?://\S+)"
    
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None
    
def process_sync_msg(msg):
    global CMD_Pointer
    """解析并处理每条同步消息"""
    # 游标机制下，能进这里的肯定都是新消息，不需要再去查 CSV 了
    
    msgtype = msg.get("msgtype")
    external_userid = msg.get("external_userid", "unknown")
    
    # 保持你要的日志格式：不换行
    print(f"[新消息] 类型={msgtype:<5} 来自={external_userid}", end="  ", flush=True)

    # 内部下载函数
    def handle_download(media_id, file_ext, type_name):
        # 游标模式下，不需要判断 is_media_downloaded
        # 直接下载
        print(f"\n   -> [下载{type_name}] {media_id}")
        timestamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime())
        filename = f"{timestamp}.{file_ext}"
        download_media_file(media_id, external_userid, filename=filename)
        time.sleep(1.1)

    if msgtype == "image":
        handle_download(msg["image"]["media_id"], "jpg", "图片")

    elif msgtype == "file":
        original_name = msg["file"].get("filename", "")
        ext = "bin"
        if "." in original_name:
            ext = original_name.split(".")[-1]
        handle_download(msg["file"]["media_id"], ext, "文件")
    
    elif msgtype == "video":
        handle_download(msg["video"]["media_id"], "mp4", "视频")
    
    elif msgtype == "text":
        content = msg.get("text", {}).get("content", "")
        print(f"-> [文本] {content}")
        if content == "重置":
            reset_cmd()
        elif content == "下载音频":
            DownloadMP3()
        elif content == "下载视频":
            DownloadMP4()    
        elif content == "保存到文件":
            SaveToDocFolder()   
        else:
            print(f"CMD_Pointer:{CMD_Pointer}")
            if CMD_Pointer == "NA":
                return
            elif  CMD_Pointer == "DownloadMP3":
                link = extract_link(content)
                download_bilibili_mp3(link)
            elif  CMD_Pointer == "DownloadMP4":
                link = extract_link(content)
                download_bilibili_mp4(link)
    elif msgtype == "location":
        print("-> [保存位置]") 
        lat = msg["location"]["latitude"]
        lon = msg["location"]["longitude"]
        save_location(external_userid, lat, lon)

    else:
        print(f"-> [忽略]")

def reset_cmd():
    global CMD_Pointer
    print("reset_cmd")
    CMD_Pointer = "NA"
    
def DownloadMP3():
    global CMD_Pointer
    print("DownloadMP3")
    CMD_Pointer = "DownloadMP3"

def DownloadMP4():
    global CMD_Pointer
    print("DownloadMP4")
    CMD_Pointer = "DownloadMP4"
    
    
def SaveToDocFolder():
    global CMD_Pointer
    print("SaveToDocFolder")
    CMD_Pointer = "SaveToDocFolder"


def download_bilibili_mp3(url):
    print(f"[开始下载] {url}")
    
    ydl_opts = {
        'format': 'bestaudio/best',  # 下载最好的音频
        'outtmpl': os.path.join(MP3_SAVE_PATH, '%(title)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }],
        'keepvideo': False,  # 🔹 关键参数：转换后删除原始文件
        'noplaylist': True,
        'quiet': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print("[下载完成] 音频已保存（源文件已删除）")
    except Exception as e:
        print(f"[下载出错] {e}")


def download_bilibili_mp4(url):
    print(f"[开始下载视频] {url}")
    
    ydl_opts = {
        # 1. 格式选择：下载最好的视频 + 最好的音频
        'format': 'bestvideo+bestaudio/best',
        
        # 2. 合并格式：强制合并为 mp4 (兼容性最好，微信能直接发)
        'merge_output_format': 'mp4',
        
        # 3. 输出路径：保存到你的指定目录，文件名使用 "标题.扩展名"
        'outtmpl': os.path.join(MP4_SAVE_PATH, '%(title)s.%(ext)s'),
        
        # 4. 其他配置
        'noplaylist': True,  # 如果是列表，只下当前这个
        'quiet': False,      # 显示日志
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print("[下载完成] 视频已保存")
        return True
    except Exception as e:
        print(f"[下载出错] {e}")
        return False

#####################################################################    
def download_media_file(media_id, from_user, ext="bin", filename=None):
    """下载文件"""
    try:
        access_token = get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
        
        resp = requests.get(url, stream=True, allow_redirects=True)
        
        if resp.status_code != 200:
            print(f"[下载失败] HTTP {resp.status_code}")
            return

        if filename is None:
            filename = f"{from_user}_{int(time.time())}.{ext}"
        filepath = os.path.join(PIC_SAVE_PATH, filename)

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        print(f"[保存成功] {filepath}")
        print(f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        # 这里移除了 mark_media_downloaded 调用，因为不需要写 CSV 了

    except Exception as e:
        print(f"[下载文件失败] {e}")


def save_location(from_user, lat, lon):
    try:
        filename = f"{from_user}_{int(time.time())}_location.txt"
        filepath = os.path.join(PIC_SAVE_PATH, filename)
        with open(filepath, "w") as f:
            f.write(f"latitude={lat}, longitude={lon}\n")
        print(f"[保存位置成功] {filepath}")
    except Exception as e:
        print(f"[保存位置失败] {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("[启动中] 企业微信客服接收服务 (游标版)")
    print("监听端口: 8888")
    print(f"文件保存目录: {os.path.abspath(PIC_SAVE_PATH)}")
    print(f"游标保存目录: {os.path.abspath(CURSOR_DIR)}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8888) 