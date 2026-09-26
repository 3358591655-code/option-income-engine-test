# Option Income Engine — Android V2

这是一个手机优先的 CSP / Covered Call 期权收入驾驶舱。

## 1. 安全

不要把 Massive API Key 写进代码。
部署时设置：

`MASSIVE_API_KEY=你的新Key`

之前已经在聊天里暴露过的 Key 应视为已经泄露并撤销；新 Key 不要发给任何人。

## 2. 本地启动（如果以后有电脑）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
export MASSIVE_API_KEY="你的新Key"
uvicorn app:app --host 0.0.0.0 --port 8000
```

然后访问：

`http://127.0.0.1:8000`

## 3. 云端部署

把整个目录部署到支持 Docker / FastAPI 的云服务。
设置环境变量：

`MASSIVE_API_KEY`

启动命令已经写入 Dockerfile。

## 4. 当前功能

- 手机响应式界面
- CSP / Covered Call
- DTE、Delta、OI、Volume、Bid/Ask、IV
- 流动性过滤
- CSP/CC/WAIT 数据状态
- 单合约历史价格路径
- API Key 后端保护

## 5. 重要数据限制

Massive 当前文档显示：
- Options API 覆盖美国期权市场；
- Options Chain Snapshot 可返回链条中的 Greeks、IV、quotes、trades、OI；
- 历史期权合约 OHLC 可用于历史分析和回测；
- 具体端点和数据权限取决于订阅等级。

因此“严格的历史 CSP/CC 回测”不能只拿今天的链条数据推算过去。
真正严格的回测应保存历史时点的：
1. underlying price
2. option chain
3. strike / expiration
4. bid / ask
5. Greeks / IV
6. OI / volume
7. 到期或平仓后的结果

V2 有意不伪造这个结果。
