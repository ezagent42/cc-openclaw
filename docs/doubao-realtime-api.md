# 豆包端到端实时语音大模型 API

## 概述

端到端 Speech-to-Speech 模型，一个 WebSocket 连接完成 ASR + LLM + TTS 全链路。
支持中文和英语，使用自定义二进制协议传输。

## WebSocket 连接

```
URL: wss://openspeech.bytedance.com/api/v3/realtime/dialogue

Required Headers:
  X-Api-App-ID: <APP_ID>          # 火山引擎控制台获取
  X-Api-Access-Key: <ACCESS_KEY>  # 火山引擎控制台获取
  X-Api-Resource-Id: volc.speech.dialog  # 固定值
  X-Api-App-Key: PlgvMymc7f3tQnJ6       # 固定值
  X-Api-Connect-Id: <UUID>              # 可选，用于追踪
```

## 二进制协议

4字节 header + optional fields + payload size (4B) + payload

### Header (4 bytes)

| Byte | Left-4bit | Right-4bit | 说明 |
|------|-----------|------------|------|
| 0 | 0b0001 (v1) | 0b0001 (4B header) | 固定 |
| 1 | Message Type | Flags | 见下表 |
| 2 | Serialization | Compression | 0=Raw, 1=JSON / 0=none, 1=gzip |
| 3 | 0x00 | Reserved | |

### Message Types

| Type | 含义 |
|------|------|
| 0b0001 | Full-client request (文本事件) |
| 0b1001 | Full-server response (文本事件) |
| 0b0010 | Audio-only request (客户端音频) |
| 0b1011 | Audio-only response (服务器音频) |
| 0b1111 | Error information |

### Flags (Message type specific)

按顺序组装 optional fields：
- sequence (4B): 事件序号
- event (4B): 事件ID
- connect id size (4B) + connect id
- session id size (4B) + session id
- payload size (4B) + payload

## 客户端事件

| ID | 名称 | 类型 | 说明 |
|----|------|------|------|
| 1 | StartConnection | Connect | WebSocket建连后声明创建连接 |
| 2 | FinishConnection | Connect | 断开连接 |
| 100 | StartSession | Session | 创建会话，配置 TTS/ASR/Dialog 参数 |
| 102 | FinishSession | Session | 结束会话 |
| 200 | TaskRequest | Data | 上传音频二进制数据 |
| 201 | UpdateConfig | Session | 更新配置 |
| 300 | SayHello | Data | 提交打招呼文本 `{"content": "xxx"}` |
| 400 | EndASR | Data | 按键模式下结束音频输入 |
| 500 | ChatTTSText | Data | 指定文本合成音频 `{"start":true,"content":"xxx","end":false}` |
| 501 | ChatTextQuery | Data | 文本query输入 `{"content": "xxx"}` |
| 502 | ChatRAGText | Data | 外部RAG知识输入 |
| 510 | ConversationCreate | Data | 初始化上下文 |
| 515 | ClientInterrupt | Data | 客户端打断 |

## 服务端事件

| ID | 名称 | 说明 |
|----|------|------|
| 50 | ConnectionStarted | 连接成功 |
| 51 | ConnectionFailed | 连接失败 `{"error": "xxx"}` |
| 52 | ConnectionFinished | 连接结束 |
| 150 | SessionStarted | 会话启动，返回 `{"dialog_id": "xxx"}` |
| 152 | SessionFinished | 会话结束 |
| 153 | SessionFailed | 会话失败 `{"error": "xxx"}` |
| 154 | UsageResponse | 用量信息 |
| 350 | TTSSentenceStart | TTS句开始 `{"tts_type":"xxx","text":"xxx","question_id":"xxx","reply_id":"xxx"}` |
| 351 | TTSSentenceEnd | TTS句结束 |
| 352 | TTSResponse | TTS音频数据（二进制payload） |
| 359 | TTSEnded | 一轮TTS结束 |
| 450 | ASRInfo | ASR首字返回（用于打断播报） |
| 451 | ASRResponse | ASR转写结果 `{"results":[{"text":"xxx","is_interim":true/false}]}` |
| 459 | ASREnded | 用户说话结束 |

## StartSession 配置

```json
{
  "tts": {
    "audio_config": {
      "format": "pcm_s16le",
      "sample_rate": 24000,
      "channel": 1
    },
    "speaker": "zh_female_vv_jupiter_bigtts"
  },
  "asr": {
    "audio_info": {
      "format": "pcm",
      "sample_rate": 16000,
      "channel": 1
    }
  },
  "dialog": {
    "bot_name": "豆包",
    "system_role": "你是一个语音助手",
    "speaking_style": "",
    "dialog_id": "",
    "extra": {
      "input_mod": "keep_alive"
    }
  },
  "extra": {
    "model": "1.2.1.1"
  }
}
```

## 音频格式

- **输入 (ASR)**: PCM int16, 16kHz, 单声道, 小端序。也支持 opus。推荐 20ms 一包。
- **输出 (TTS)**: 默认 OGG Opus。可配置为 PCM (24kHz, 32bit 或 16bit)。

## 模型版本

- `1.2.1.1` — O2.0版本（S2S-Omni，通用助手）
- `2.2.0.0` — SC2.0版本（S2S-Strong Character，角色扮演）

## 音色

O版本默认音色：
- `zh_female_vv_jupiter_bigtts` — vv，活泼灵动女声
- `zh_female_xiaohe_jupiter_bigtts` — xiaohe，甜美活泼女声
- `zh_male_yunzhou_jupiter_bigtts` — yunzhou，清爽沉稳男声
- `zh_male_xiaotian_jupiter_bigtts` — xiaotian，清爽磁性男声

## 最佳实践

1. 麦克风输入推荐 20ms 一包发送
2. 麦克风含静音按键时设置 `input_mod: "keep_alive"`
3. 按键说话模式设置 `input_mod: "push_to_talk"`
4. 纯文本输入设置 `input_mod: "text"`
5. FinishSession 后 WebSocket 可复用
6. 限流：60 QPM (StartSession/分钟)，10000 TPM (tokens/分钟)
