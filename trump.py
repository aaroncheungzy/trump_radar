import requests
import time
import hashlib
import json
import logging
import re
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from dateutil import parser, tz
from bs4 import BeautifulSoup
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from logging.handlers import RotatingFileHandler
import markdown
import yfinance as yf  # 导入yfinance库

# 导入豆包官方SDK
from volcenginesdkarkruntime import Ark


# 获取当前代码文件目录
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 配置日志
log_file_path = os.path.join(CURRENT_DIR, "news_monitor.log")
file_handler = RotatingFileHandler(
    log_file_path,
    maxBytes=1024*1024*5,  # 5MB
    backupCount=5,
    encoding='utf-8'
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
    handlers=[file_handler, logging.StreamHandler()]
)

class TimeoutException(Exception):
    """自定义超时异常"""
    pass

def timeout_wrapper(func, args=(), kwargs={}, timeout=30):
    """函数超时包装器"""
    result = [None]
    exception = [None]
    
    def target():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            exception[0] = e
    
    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout)
    
    if thread.is_alive():
        raise TimeoutException(f"函数执行超时（超过{timeout}秒）")
    if exception[0]:
        raise exception[0]
    return result[0]

# 自定义JSON编码器：处理datetime类型
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()  # 将datetime转为ISO格式字符串
        return super().default(obj)

class NewsMonitor:
    def __init__(self, config, thresholds, financial_assets):
        self.target_url = config["target_url"]
        self.summary_times = config["summary_times"]  # 每日检查时间（北京时间）
        self.history_file = os.path.join(CURRENT_DIR, config["history_file"])
        self.deepseek_api_key = config["deepseek_api_key"]
        self.doubao_api_key = config.get("doubao_api_key")  # 豆包API密钥
        
        # 金融资产配置（指数和虚拟货币）
        self.financial_assets = financial_assets  # 格式: {代码: {名称: ..., 类型: ...}}
        
        # 阈值参数
        self.CONTENT_LENGTH_THRESHOLD = thresholds["content_length"]
        self.MAX_RECENT_IDS = thresholds["max_recent_ids"]
        self.API_RETRY_TIMES = thresholds["api_retry_times"]
        self.DEEPSEEK_TIMEOUT = thresholds["deepseek_timeout"]
        self.DOUBAO_TIMEOUT = thresholds.get("doubao_timeout", 60)  # 豆包超时时间
        self.FINANCIAL_DATA_TIMEOUT = thresholds.get("financial_data_timeout", 30)  # 金融数据获取超时
        
        # 邮件配置
        self.email_config = {
            "smtp_server": config["email_smtp_server"],
            "smtp_port": config["email_smtp_port"],
            "sender_email": config["sender_email"],
            "sender_password": config["sender_password"],
            "receiver_emails": config["email_receivers"]
        }
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Accept": "application/xml, text/xml, */*"
        }
        
        # API配置（优先豆包，备用DeepSeek）
        self.deepseek_api_url = "https://api.deepseek.com/v1/chat/completions"
        self.deepseek_model = "deepseek-chat"
        self.doubao_client = Ark(api_key=self.doubao_api_key)  # 豆包官方SDK客户端
        self.doubao_model = "doubao-seed-1-6-251015"  # 豆包模型名称
        
        # 时区配置
        self.utc_tz = timezone.utc
        self.washington_tz = tz.gettz('America/New_York')
        self.beijing_tz = tz.gettz('Asia/Shanghai')
        
        # 初始化会话
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # 初始化数据
        self.processed_articles = self._load_history()
        self.recent_article_ids = set()
        self.last_modified = None
        
        # 解析汇总时间
        self.summary_schedules = []
        for t in self.summary_times:
            hour, minute = map(int, t.split(":"))
            self.summary_schedules.append((hour, minute))
        
        # 启动时测试模型连接
        self.test_model_connections()
        
        logging.info(f"初始化完成 - 每日检查时间（北京时间）：{self.summary_times}")
        logging.info(f"每次检查处理所有未处理过的新文章")
        logging.info(f"监控的金融资产: {[v['name'] for v in self.financial_assets.values()]}")
        logging.info(f"优先使用模型: {self.doubao_model}, 备用模型: {self.deepseek_model}")

    def _load_history(self):
        """加载已处理文章的历史记录"""
        try:
            if not os.path.exists(self.history_file):
                logging.info("历史记录文件不存在，创建新文件")
                with open(self.history_file, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
                return {}
                
            with open(self.history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"加载历史记录失败: {e}")
            return {}

    def _save_history(self):
        """保存已处理文章的历史记录"""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.processed_articles, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"保存历史记录失败: {e}")

    def _generate_article_id(self, title, pub_date):
        """生成文章唯一ID"""
        cleaned_title = re.sub(r'\s+', '', title.strip())
        return hashlib.md5(f"{cleaned_title}|{pub_date}".encode()).hexdigest()

    def _is_content_valid(self, content):
        """判断内容是否有效（字母数量大于阈值）"""
        if not content:
            return False, 0
        letters_only = re.sub(r'[^a-zA-Z]', '', content)
        length = len(letters_only)
        return length > self.CONTENT_LENGTH_THRESHOLD, length

    def _is_new_article(self, article_id):
        """判断是否为未处理的新文章"""
        return article_id not in self.recent_article_ids and article_id not in self.processed_articles

    def _fetch_webpage_with_retry(self, url):
        """获取网页内容（带重试机制，失败后每5分钟重试一次）"""
        while True:
            try:
                logging.debug(f"获取网页: {url}")
                headers = self.headers.copy()
                if self.last_modified:
                    headers['If-Modified-Since'] = self.last_modified
                    
                response = self.session.get(url, headers=headers, timeout=15)
                if response.status_code == 304:
                    logging.info("内容未更新，无需处理")
                    return None
                    
                self.last_modified = response.headers.get('Last-Modified')
                response.raise_for_status()
                
                # 调试用：保存原始HTML
                debug_html_path = os.path.join(CURRENT_DIR, "debug_raw_html.html")
                with open(debug_html_path, "w", encoding="utf-8") as f:
                    f.write(response.text)
                return response.text
            except Exception as e:
                logging.error(f"获取网页失败: {e}，将在5分钟后重试")
                time.sleep(300)  # 5分钟后重试

    def _extract_articles_with_pubdate(self, html):
        """提取有效文章（带发布时间、过滤短内容）"""
        if not html:
            logging.debug("无网页内容可提取")
            return []
        
        articles = []
        try:
            soup = BeautifulSoup(html, 'xml')
            items = soup.find_all('item')
            
            if not items:
                logging.warning("未找到任何文章项(item标签)")
                return []
                
            for item in items:
                # 提取发布时间
                pub_date_tag = item.find('pubDate')
                if not pub_date_tag:
                    logging.debug("跳过无发布时间的文章")
                    continue
                pub_date_str = pub_date_tag.get_text(strip=True)
                
                # 提取标题和内容
                title_tag = item.find('title')
                title = title_tag.get_text(strip=True) if title_tag else "无标题"
                
                desc_tag = item.find('description')
                if not desc_tag:
                    logging.debug("跳过无内容的文章")
                    continue
                raw_content = desc_tag.get_text(strip=True)
                cleaned_content = re.sub(r'\s+', ' ', raw_content).strip()
                
                # 过滤短内容
                is_valid, _ = self._is_content_valid(cleaned_content)
                if not is_valid:
                    logging.debug(f"跳过短内容文章（标题：{title}）")
                    continue
                
                # 解析为UTC时间对象
                try:
                    pub_datetime_utc = parser.parse(pub_date_str).replace(tzinfo=self.utc_tz)
                except Exception as e:
                    logging.warning(f"时间解析失败 {pub_date_str}: {e}")
                    continue
                
                articles.append({
                    "title": title,
                    "content": cleaned_content,
                    "pub_date": pub_date_str,
                    "pub_datetime": pub_datetime_utc
                })
            
            # 按发布时间降序排序
            articles.sort(key=lambda x: x["pub_datetime"], reverse=True)
            logging.info(f"成功提取{len(articles)}篇有效文章（已过滤短内容）")
            
        except Exception as e:
            logging.error(f"提取文章失败: {e}", exc_info=True)
        
        return articles

    def _get_all_new_articles(self, all_articles):
        """筛选所有未处理的新文章（取消时间窗口限制）"""
        new_articles = []
        
        for article in all_articles:
            # 检查是否为未处理的新文章（不限制时间）
            article_id = self._generate_article_id(article["title"], article["pub_date"])
            if self._is_new_article(article_id):
                logging.debug(f"发现新文章: {article['title']}（ID: {article_id}）")
                new_articles.append(article)
                # 临时记录已处理ID（避免同批次重复）
                self.recent_article_ids.add(article_id)
        
        # 去重并排序
        unique_articles = []
        seen_ids = set()
        for art in new_articles:
            art_id = self._generate_article_id(art["title"], art["pub_date"])
            if art_id not in seen_ids:
                seen_ids.add(art_id)
                unique_articles.append(art)
        unique_articles.sort(key=lambda x: x["pub_datetime"], reverse=True)
        
        return unique_articles

    def _get_financial_data(self):
        """获取金融资产数据（当前价格、24小时涨跌幅）"""
        financial_data = {}
        current_time = datetime.now(self.beijing_tz)
        
        for symbol, info in self.financial_assets.items():
            try:
                logging.info(f"获取{info['name']}({symbol})的金融数据...")
                
                # 使用yfinance获取数据（最多重试3次）
                retry_count = 0
                while retry_count < 3:
                    try:
                        # 获取最近1天数据（间隔1小时）
                        ticker = yf.Ticker(symbol)
                        hist = ticker.history(period="1d", interval="1h")
                        
                        if hist.empty:
                            # 尝试获取实时价格
                            price = ticker.info.get('regularMarketPrice') or ticker.info.get('currentPrice')
                            if price is None:
                                raise Exception("无法获取价格数据")
                            
                            # 假设24小时前价格（实际应用中可优化为获取历史数据）
                            prev_hist = ticker.history(period="2d", interval="1d")
                            if len(prev_hist) >= 2:
                                prev_price = prev_hist['Close'].iloc[-2]
                            else:
                                prev_price = price  # 无法获取历史数据时默认无变化
                        else:
                            # 最新价格
                            price = hist['Close'].iloc[-1]
                            # 24小时前价格（取最早的记录）
                            prev_price = hist['Close'].iloc[0]
                        
                        # 计算涨跌幅
                        change = price - prev_price
                        change_percent = (change / prev_price) * 100 if prev_price != 0 else 0
                        
                        financial_data[symbol] = {
                            "name": info["name"],
                            "type": info["type"],
                            "current_price": round(price, 4),
                            "change_24h": round(change, 4),
                            "change_percent_24h": round(change_percent, 2),
                            "update_time": current_time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                        break
                        
                    except Exception as e:
                        retry_count += 1
                        logging.warning(f"获取{info['name']}数据失败（第{retry_count}次重试）: {e}")
                        if retry_count < 3:
                            time.sleep(2)  # 重试间隔
                        else:
                            financial_data[symbol] = {
                                "name": info["name"],
                                "type": info["type"],
                                "error": f"获取数据失败: {str(e)}"
                            }
                
            except Exception as e:
                logging.error(f"处理{info['name']}数据时发生错误: {e}")
                financial_data[symbol] = {
                    "name": info["name"],
                    "type": info["type"],
                    "error": f"处理失败: {str(e)}"
                }
        
        return financial_data

    def _call_doubao_api(self, prompt):
        """调用豆包官方SDK（优先使用）"""
        if not self.doubao_api_key:
            raise Exception("豆包API密钥未配置")
            
        try:
            # 豆包官方调用格式
            response = self.doubao_client.chat.completions.create(
                model=self.doubao_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=2048,
                response_format={"type": "text"}
            )
            return response.choices[0].message.content
        
        # 通用异常捕获（兼容所有SDK版本）
        except Exception as e:
            logging.error(f"豆包SDK调用失败: {str(e)}")
            raise

    def _call_deepseek_api(self, prompt):
        """调用DeepSeek API（备用）"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.deepseek_api_key}"
        }
        
        data = {
            "model": self.deepseek_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "max_tokens": 2048,
            "response_format": {"type": "text"}
        }
        
        try:
            response = self.session.post(
                self.deepseek_api_url,
                headers=headers,
                json=data,
                timeout=self.DEEPSEEK_TIMEOUT
            )
            response.raise_for_status()
            result = response.json()
            
            if "choices" in result and len(result["choices"]) > 0:
                return result["choices"][0]["message"]["content"]
            return "【DeepSeek API返回格式异常】"
            
        except Exception as e:
            logging.error(f"DeepSeek API调用失败: {e}")
            raise

    def _call_llm_api(self, prompt):
        """优先调用豆包，失败则调用DeepSeek"""
        try:
            logging.info(f"尝试调用优先模型: {self.doubao_model}")
            return timeout_wrapper(
                self._call_doubao_api, 
                args=(prompt,), 
                timeout=self.DOUBAO_TIMEOUT
            )
        except Exception as e:
            logging.warning(f"优先模型调用失败，切换到备用模型: {str(e)}")
            try:
                logging.info(f"尝试调用备用模型: {self.deepseek_model}")
                return timeout_wrapper(
                    self._call_deepseek_api, 
                    args=(prompt,), 
                    timeout=self.DEEPSEEK_TIMEOUT
                )
            except TimeoutException:
                return f"【所有模型调用超时（豆包: {self.DOUBAO_TIMEOUT}秒, DeepSeek: {self.DEEPSEEK_TIMEOUT}秒）】"
            except Exception as e2:
                return f"【所有模型调用失败: {str(e2)}】"

    def test_model_connections(self):
        """测试所有模型是否能正常调用"""
        logging.info("\n===== 开始模型连通性测试 =====")
        
        # 测试豆包
        doubao_ok = False
        try:
            logging.info(f"测试 豆包 模型: {self.doubao_model}")
            response = self.doubao_client.chat.completions.create(
                model=self.doubao_model,
                messages=[{"role": "user", "content": "测试连接，返回'OK'即可"}],
                max_tokens=5
            )
            if response.choices[0].message.content.strip() == "OK":
                doubao_ok = True
                logging.info(f"✅ 豆包 模型调用成功")
            else:
                logging.warning(f"❌ 豆包 响应异常: {response.choices[0].message.content}")
        except Exception as e:  # 通用异常捕获
            logging.error(f"❌ 豆包 调用失败: {str(e)}")
        
        # 测试DeepSeek
        deepseek_ok = False
        try:
            logging.info(f"测试 DeepSeek 模型: {self.deepseek_model}")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.deepseek_api_key}"
            }
            data = {
                "model": self.deepseek_model,
                "messages": [{"role": "user", "content": "测试连接，返回'OK'即可"}],
                "max_tokens": 5
            }
            response = self.session.post(
                self.deepseek_api_url,
                headers=headers,
                json=data,
                timeout=self.DEEPSEEK_TIMEOUT
            )
            response.raise_for_status()
            result = response.json()
            if result.get("choices", []) and result["choices"][0]["message"]["content"].strip() == "OK":
                deepseek_ok = True
                logging.info(f"✅ DeepSeek 模型调用成功")
            else:
                logging.warning(f"❌ DeepSeek 响应异常: {result}")
        except Exception as e:
            logging.error(f"❌ DeepSeek 调用失败: {str(e)}")
        
        # 总结测试结果
        if doubao_ok and deepseek_ok:
            logging.info("===== 所有模型测试通过 =====")
        elif doubao_ok:
            logging.warning("===== 仅备用模型测试失败，优先模型可用 =====")
        elif deepseek_ok:
            logging.warning("===== 仅优先模型测试失败，备用模型可用 =====")
        else:
            logging.error("===== 所有模型测试失败，程序可能无法正常工作 =====")

    def _translate_single_article(self, article):
        """单篇文章翻译（使用优先模型）"""
        prompt = f"""请将以下文章准确翻译成中文（不用写注释）：
文章内容：{article['content']}"""
        return self._call_llm_api(prompt)

    def _translate_articles_batch(self, articles):
        """批量翻译文章（按单篇拆分返回）"""
        translations = []
        for i, article in enumerate(articles, 1):
            logging.info(f"正在翻译第{i}/{len(articles)}篇文章...")
            trans = self._translate_single_article(article)
            pub_time_beijing = article["pub_datetime"].astimezone(self.beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
            pub_time_washington = article["pub_datetime"].astimezone(self.washington_tz).strftime("%Y-%m-%d %H:%M:%S")
            translations.append({
                "article_index": i,
                "original_content": article["content"],
                "pub_time_beijing": pub_time_beijing,
                "pub_time_washington": pub_time_washington,
                "chinese_translation": trans
            })
        return translations

    def _summarize_economic_impact(self, translations, financial_data):
        """汇总所有文章的经济影响分析（结合金融数据）"""
        if not translations:
            return "【无有效文章可分析】"
        
        # 格式化金融数据
        financial_str = "当前金融市场数据（分析时点）：\n"
        for data in financial_data.values():
            if "error" in data:
                financial_str += f"- {data['name']}: {data['error']}\n"
            else:
                change_sign = "+" if data["change_24h"] >= 0 else ""
                financial_str += (f"- {data['name']}: 当前价格 {data['current_price']}，"
                                 f"24小时变动 {change_sign}{data['change_24h']} "
                                 f"({change_sign}{data['change_percent_24h']}%)\n")
        
        combined_translations = ""
        for trans in translations:
            combined_translations += f"文章{trans['article_index']}翻译内容：{trans['chinese_translation']}\n\n"
        
        prompt = f"""基于以下所有文章的翻译内容和当前金融市场数据，进行经济影响汇总分析：
要求：
1. 先简明扼要总结所有文章；
2. 结合提供的金融数据，分析对美国经济及相关市场的整体潜在影响，解释影响逻辑和传导路径，指出关键风险或机遇；
3. 给出对投资者的针对性建议；
4. 编号使用1.，2.，以此类推；
5. 语言专业、客观，逻辑清晰，不超过500字。

{financial_str}

{combined_translations}"""
        
        return self._call_llm_api(prompt)

    def _send_summary_email(self, articles, translations, impact_summary, financial_data):
        """发送汇总分析邮件（包含金融数据）"""
        try:
            summary_time = datetime.now(self.beijing_tz).strftime("%Y-%m-%d %H:%M")
            subject = f"【{summary_time} 新闻汇总分析】共{len(articles)}篇新文章"
            
            earliest_time = min(art['pub_datetime'] for art in articles) if articles else None
            latest_time = max(art['pub_datetime'] for art in articles) if articles else None
            
            # 时间范围信息
            time_info = ""
            if earliest_time and latest_time:
                time_info = f"""
                <p><strong>文章发布时间范围</strong>：
                <br>华盛顿时间：{earliest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')}
                <br>北京时间：{earliest_time.astimezone(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.astimezone(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')}
                </p>
                """
            
            # 金融数据表格
            financial_html = "<p><strong>三、金融市场数据（分析时点）</strong></p>"
            financial_html += """
            <table border="1" cellpadding="8" style="border-collapse:collapse; width:100%;">
                <tr style="background-color:#f2f2f2;">
                    <th>资产名称</th>
                    <th>类型</th>
                    <th>当前价格</th>
                    <th>24小时变动</th>
                    <th>24小时变动百分比</th>
                </tr>
            """
            for data in financial_data.values():
                financial_html += "<tr>"
                financial_html += f"<td>{data['name']}</td>"
                financial_html += f"<td>{data['type']}</td>"
                
                if "error" in data:
                    financial_html += f"<td colspan='3'>{data['error']}</td>"
                else:
                    # 涨跌颜色标记（涨红跌绿）
                    change_style = "color:red;" if data["change_24h"] >= 0 else "color:green;"
                    percent_style = "color:red;" if data["change_percent_24h"] >= 0 else "color:green;"
                    
                    financial_html += f"<td>{data['current_price']}</td>"
                    financial_html += f"<td style='{change_style}'>{data['change_24h']}</td>"
                    financial_html += f"<td style='{percent_style}'>{data['change_percent_24h']}%</td>"
                financial_html += "</tr>"
            financial_html += "</table>"
            
            # 文章内容
            articles_html = "<p><strong>一、原文+翻译</strong></p>"
            if not translations:
                articles_html += "<p>无新文章</p>"
            else:
                for trans in translations:
                    articles_html += f"""
                    <div style="margin: 15px 0; padding: 10px; border: 1px solid #eee; border-radius: 4px;">
                        <p><strong>文章 {trans['article_index']}</strong></p>
                        <p><strong>发布时间：</strong>
                        北京时间 {trans['pub_time_beijing']}<br>
                        华盛顿时间 {trans['pub_time_washington']}
                        </p>
                        <p><strong>原文内容：</strong>{trans['original_content']}</p>
                        <p><strong>中文翻译：</strong>{trans['chinese_translation']}</p>
                    </div>
                    """
            
            # 分析内容
            summary_html = f"""
            <p><strong>二、经济影响汇总分析</strong></p>
            <p>{markdown.markdown(impact_summary)}</p>
            """
            
            email_body = f"""
            <p>您好，以下是新文章的汇总分析结果（{summary_time} 北京时间）：</p>
            {time_info}
            {articles_html}
            {summary_html}
            {financial_html}
            <p>此邮件为自动发送，请勿回复。</p>
            """
            
            msg = MIMEMultipart()
            msg['From'] = self.email_config["sender_email"]
            msg['To'] = ", ".join(self.email_config["receiver_emails"])
            msg['Subject'] = subject
            msg.attach(MIMEText(email_body, 'html', 'utf-8'))
            
            smtp_server = self.email_config["smtp_server"]
            smtp_port = self.email_config["smtp_port"]
            
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
                    server.login(self.email_config["sender_email"], self.email_config["sender_password"])
                    server.sendmail(
                        self.email_config["sender_email"],
                        self.email_config["receiver_emails"],
                        msg.as_string()
                    )
            elif smtp_port == 587:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(self.email_config["sender_email"], self.email_config["sender_password"])
                    server.sendmail(
                        self.email_config["sender_email"],
                        self.email_config["receiver_emails"],
                        msg.as_string()
                    )
            else:
                raise Exception(f"不支持的SMTP端口：{smtp_port}，请使用465或587")
            
            logging.info(f"汇总分析邮件发送成功！收件人：{msg['To']}")
            return True
        
        except Exception as e:
            logging.error(f"邮件发送失败: {str(e)}", exc_info=True)
            return False

    def _process_summary_batch(self, articles):
        """处理汇总批次文章（翻译+分析+金融数据+输出+邮件）"""
        # 获取金融数据
        financial_data = self._get_financial_data()
        
        if not articles:
            logging.info("无新文章，仅输出金融数据")
            # 控制台输出金融数据
            self._print_financial_data(financial_data)
            return
            
        logging.info(f"开始处理汇总批次（共{len(articles)}篇文章）...")
        
        # 1. 按单篇翻译
        translations = self._translate_articles_batch(articles)
        
        # 2. 汇总经济影响分析（结合金融数据）
        impact_summary = self._summarize_economic_impact(translations, financial_data)
        
        # 3. 控制台输出
        print("\n" + "="*100)
        print(f"【{datetime.now(self.beijing_tz).strftime('%Y-%m-%d %H:%M')} 汇总分析】共{len(articles)}篇新文章")
        
        earliest_time = min(art['pub_datetime'] for art in articles)
        latest_time = max(art['pub_datetime'] for art in articles)
        print(f"\n【文章发布时间范围】")
        print(f"北京时间：{earliest_time.astimezone(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.astimezone(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"华盛顿时间：{earliest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 输出金融数据
        self._print_financial_data(financial_data)
        
        print("\n【文章详情（原文+翻译）】")
        for trans in translations:
            print(f"\n{'-'*80}")
            print(f"文章 {trans['article_index']}")
            print(f"发布时间：北京时间 {trans['pub_time_beijing']} | 华盛顿时间 {trans['pub_time_washington']}")
            print(f"原文：{trans['original_content'][:500]}..." if len(trans['original_content'])>500 else f"原文：{trans['original_content']}")
            print(f"翻译：{trans['chinese_translation'][:500]}..." if len(trans['chinese_translation'])>500 else f"翻译：{trans['chinese_translation']}")
        
        print(f"\n{'-'*80}")
        print("\n【经济影响汇总分析】")
        print(impact_summary)
        print("\n" + "="*100 + "\n")
        
        # 4. 标记为已处理
        for article in articles:
            article_id = self._generate_article_id(article["title"], article["pub_date"])
            self.processed_articles[article_id] = article["pub_date"]
        self._save_history()
        
        # 5. 发送邮件
        self._send_summary_email(articles, translations, impact_summary, financial_data)

    def _print_financial_data(self, financial_data):
        """控制台输出金融数据"""
        print("\n" + "-"*80)
        print("【金融市场数据（分析时点）】")
        print(f"数据更新时间: {datetime.now(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'资产名称':<15} {'类型':<6} {'当前价格':<12} {'24小时变动':<12} {'24小时变动百分比':<10}")
        print("-"*80)
        for data in financial_data.values():
            if "error" in data:
                print(f"{data['name']:<15} {data['type']:<6} {data['error']}")
            else:
                change_sign = "+" if data["change_24h"] >= 0 else ""
                percent_sign = "+" if data["change_percent_24h"] >= 0 else ""
                print(f"{data['name']:<15} {data['type']:<6} {data['current_price']:<12} "
                      f"{change_sign}{data['change_24h']:<11} {percent_sign}{data['change_percent_24h']}%")
        print("-"*80)

    def _execute_scheduled_check(self):
        """执行定时检查：获取文章+筛选+处理"""
        now_bj = datetime.now(self.beijing_tz)
        logging.info(f"开始执行定时检查（{now_bj.strftime('%Y-%m-%d %H:%M:%S')} 北京时间）")
        
        # 1. 获取网页内容（带重试机制）
        html = self._fetch_webpage_with_retry(self.target_url)
        if not html:
            # 即使没有新文章也获取并输出金融数据
            financial_data = self._get_financial_data()
            self._print_financial_data(financial_data)
            logging.info("未获取到新内容，本次检查结束")
            return
            
        # 2. 提取有效文章
        all_articles = self._extract_articles_with_pubdate(html)
        
        # 3. 筛选所有未处理的新文章（取消时间窗口限制）
        new_articles = self._get_all_new_articles(all_articles) if all_articles else []
        
        # 4. 处理汇总
        self._process_summary_batch(new_articles)
        
        # 5. 清空临时ID缓存
        self.recent_article_ids.clear()
        logging.info("本次定时检查完成")

    def start_scheduler(self):
        """启动调度器：仅在指定时间点执行检查"""
        logging.info(f"开始定时调度，检查时间（北京时间）：{self.summary_times}")
        try:
            while True:
                now_bj = datetime.now(self.beijing_tz)
                current_hour, current_minute = now_bj.hour, now_bj.minute
                
                # 检查是否匹配任意指定时间（允许±1分钟误差）
                for (target_hour, target_minute) in self.summary_schedules:
                    if (abs(current_hour - target_hour) == 0 and 
                        abs(current_minute - target_minute) <= 1):
                        
                        # 执行定时检查
                        self._execute_scheduled_check()
                        # 避免同一时间点重复执行（等待2分钟）
                        time.sleep(120)
                        break
                
                # 每30秒检查一次时间
                time.sleep(30)
                
        except KeyboardInterrupt:
            logging.info("程序手动退出")
        except Exception as e:
            logging.error(f"调度器终止: {e}", exc_info=True)


if __name__ == "__main__":
    # 配置代理（根据实际情况修改或删除）
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

    # 阈值参数集中配置
    thresholds = {
        "content_length": 50,
        "max_recent_ids": 1000,
        "deepseek_timeout": 60,
        "doubao_timeout": 60,
        "api_retry_times": 3,
        "financial_data_timeout": 30  # 金融数据获取超时时间
    }

    # 配置需要监控的金融资产（用户可在此修改）
    # 格式: {yfinance代码: {"name": "资产名称", "type": "类型"}}
    financial_assets = {
        "^IXIC": {"name": "纳斯达克指数", "type": "指数"},
        "BTC-USD": {"name": "比特币", "type": "加密货币"},
        "ETH-USD": {"name": "以太币", "type": "加密货币"},
        "SOL-USD": {"name": "SOL", "type": "加密货币"},
        "XRP-USD": {"name": "XRP", "type": "加密货币"}
    }

    # 核心配置参数
    config = {
        "target_url": "https://trumpstruth.org/feed",
        "summary_times": ["10:00", "22:00"],
        "history_file": "processed_articles.json",
        "deepseek_api_key": "sk-0b92022c288948a5a6f46fb1034587be",
        "doubao_api_key": "c6a0e5ac-3c2e-41ab-a261-1cbc458ca07d",  # 替换为实际豆包API密钥
        
        # 邮件配置
        "email_smtp_server": "smtp.mail.me.com",
        "email_smtp_port": 587,
        "sender_email": "aaroncheungzy@icloud.com",
        "sender_password": "uyss-qnpl-xpae-fpuu",
        "email_receivers": ["aaroncheungzy@icloud.com", "zhangyang963@pingan.com.cn"]
    }

    # 启动调度器
    monitor = NewsMonitor(config, thresholds, financial_assets)
    monitor.start_scheduler()