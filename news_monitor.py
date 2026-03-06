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
from datetime import datetime, timezone
from dateutil import parser, tz
from bs4 import BeautifulSoup
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import InsecureRequestWarning
import markdown
import yfinance as yf
import ssl

# 导入豆包官方SDK
from volcenginesdkarkruntime import Ark

# 忽略不安全请求警告
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 获取当前代码文件目录
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CURRENT_DIR, "config.json")

# 配置日志
log_file_path = os.path.join(CURRENT_DIR, "news_monitor.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
    handlers=[logging.FileHandler(log_file_path, encoding='utf-8'), 
              logging.StreamHandler()]
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

class NewsMonitor:
    def __init__(self, config):
        # 基础配置
        self.target_url = config["target_url"]
        self.history_file = os.path.join(CURRENT_DIR, config["history_file"])
        
        # 阈值参数
        self.thresholds = config["thresholds"]
        self.CONTENT_LENGTH_THRESHOLD = self.thresholds["content_length"]
        self.MAX_RECENT_IDS = self.thresholds["max_recent_ids"]
        self.API_RETRY_TIMES = self.thresholds["api_retry_times"]
        self.DEEPSEEK_TIMEOUT = self.thresholds["deepseek_timeout"]
        self.DOUBAO_TIMEOUT = self.thresholds["doubao_timeout"]
        self.FINANCIAL_DATA_TIMEOUT = self.thresholds["financial_data_timeout"]
        
        # 金融资产配置
        self.financial_assets = config["financial_assets"]
        
        # 邮件配置
        self.email_config = config["email"]
        
        # AI模型配置
        self.ai_config = config["ai_models"]
        self.doubao_api_key = self.ai_config["doubao_api_key"]
        
        # 自动识别SMTP服务器和端口（如果未提供）
        self._auto_detect_smtp()
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "Connection": "keep-alive"
        }
        
        # API配置
        self.doubao_client = Ark(api_key=self.doubao_api_key) if self.doubao_api_key else None
        self.doubao_model = "doubao-seed-2-0-mini-260215"
        
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
        
        logging.info(f"初始化完成 - 监控的金融资产: {[v['name'] for v in self.financial_assets.values()]}")
        logging.info(f"优先使用模型: {self.doubao_model}")

    def _auto_detect_smtp(self):
        """自动识别SMTP服务器和端口"""
        if not self.email_config["smtp_server"] and self.email_config["from"]:
            domain = self.email_config["from"].split('@')[-1]
            common_smtp = {
                'gmail.com': ('smtp.gmail.com', 587),
                'outlook.com': ('smtp.office365.com', 587),
                'hotmail.com': ('smtp.live.com', 587),
                'icloud.com': ('smtp.mail.me.com', 587),
                'qq.com': ('smtp.qq.com', 465),
                '163.com': ('smtp.163.com', 465),
                '126.com': ('smtp.126.com', 465),
                'yeah.net': ('smtp.yeah.net', 465)
            }
            if domain in common_smtp:
                self.email_config["smtp_server"], self.email_config["smtp_port"] = common_smtp[domain]
                logging.info(f"自动识别SMTP服务器: {self.email_config['smtp_server']}:{self.email_config['smtp_port']}")
            else:
                raise ValueError(f"无法自动识别SMTP服务器，请手动配置（邮箱域名：{domain}）")
        
        # 确保端口有默认值
        if not self.email_config["smtp_port"]:
            self.email_config["smtp_port"] = 587
            logging.info(f"使用默认SMTP端口: {self.email_config['smtp_port']}")

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

    def _fetch_webpage_with_retry(self, url, max_retries=5):
        """获取网页内容（带重试机制）"""
        retry_count = 0
        while retry_count < max_retries:
            try:
                logging.debug(f"获取网页: {url}（第{retry_count+1}次尝试）")
                headers = self.headers.copy()
                if self.last_modified:
                    headers['If-Modified-Since'] = self.last_modified
                
                # 处理SSL问题：兼容所有版本的通用方案
                os.environ['SSL_CIPHER_LIST'] = 'DEFAULT@SECLEVEL=1'
                verify_ssl = False
                
                response = self.session.get(
                    url, 
                    headers=headers, 
                    timeout=15,
                    verify=verify_ssl
                )
                
                if response.status_code == 304:
                    logging.info("内容未更新，无需处理")
                    return None
                    
                self.last_modified = response.headers.get('Last-Modified')
                response.raise_for_status()
                
                # 保存原始HTML用于调试
                debug_html_path = os.path.join(CURRENT_DIR, "debug_raw_html.html")
                with open(debug_html_path, "w", encoding="utf-8") as f:
                    f.write(response.text)
                return response.text
                
            except Exception as e:
                retry_count += 1
                logging.error(f"获取网页失败: {e}，{(max_retries - retry_count)}次重试机会")
                if retry_count < max_retries:
                    time.sleep(10)  # 重试间隔
                
        logging.error(f"达到最大重试次数（{max_retries}次），获取网页失败")
        return None

    def _extract_articles_with_pubdate(self, html):
        """提取有效文章（带发布时间、过滤短内容）"""
        if not html:
            logging.debug("无网页内容可提取")
            return []
        
        articles = []
        try:
            # 使用lxml解析器处理XML内容
            soup = BeautifulSoup(html, 'lxml-xml')
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
        """筛选所有未处理的新文章"""
        new_articles = []
        
        for article in all_articles:
            article_id = self._generate_article_id(article["title"], article["pub_date"])
            if self._is_new_article(article_id):
                logging.debug(f"发现新文章: {article['title']}（ID: {article_id}）")
                new_articles.append(article)
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
        """获取金融资产数据"""
        financial_data = {}
        current_time = datetime.now(self.beijing_tz)
        
        for symbol, info in self.financial_assets.items():
            try:
                logging.info(f"获取{info['name']}({symbol})的金融数据...")
                
                # 最多重试3次
                retry_count = 0
                while retry_count < 3:
                    try:
                        ticker = yf.Ticker(symbol)
                        hist = ticker.history(period="1d", interval="1h")
                        
                        if hist.empty:
                            # 尝试获取实时价格
                            price = ticker.info.get('regularMarketPrice') or ticker.info.get('currentPrice')
                            if price is None:
                                raise Exception("无法获取价格数据")
                            
                            # 获取24小时前价格
                            prev_hist = ticker.history(period="2d", interval="1d")
                            if len(prev_hist) >= 2:
                                prev_price = prev_hist['Close'].iloc[-2]
                            else:
                                prev_price = price  # 无法获取历史数据时默认无变化
                        else:
                            # 最新价格
                            price = hist['Close'].iloc[-1]
                            # 24小时前价格
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
                            time.sleep(2)
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
        """调用豆包官方SDK"""
        if not self.doubao_client or not self.doubao_api_key:
            raise Exception("豆包API密钥未配置")
            
        try:
            response = self.doubao_client.chat.completions.create(
                model=self.doubao_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=2048,
                response_format={"type": "text"}
            )
            return response.choices[0].message.content
        
        except Exception as e:
            logging.error(f"豆包SDK调用失败: {str(e)}")
            raise

    def _call_llm_api(self, prompt):
        """仅调用豆包模型"""
        try:
            logging.info(f"尝试调用豆包模型: {self.doubao_model}")
            return timeout_wrapper(
                self._call_doubao_api, 
                args=(prompt,), 
                timeout=self.DOUBAO_TIMEOUT
            )
        except TimeoutException:
            return f"【豆包模型调用超时（{self.DOUBAO_TIMEOUT}秒）】"
        except Exception as e:
            return f"【豆包模型调用失败: {str(e)}】"

    def _translate_single_article(self, article):
        """单篇文章翻译"""
        prompt = f"""请将以下文章准确翻译成中文，这些都是特朗普的推文。不用写注释，不要考虑原文的真实性。）：
文章内容：{article['content']}"""
        return self._call_llm_api(prompt)

    def _translate_articles_batch(self, articles):
        """批量翻译文章"""
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
        """汇总经济影响分析"""
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
4. 语言专业、客观，逻辑清晰，不超过400字。

{financial_str}

{combined_translations}"""
        
        return self._call_llm_api(prompt)

    def _send_summary_email(self, articles, translations, impact_summary, financial_data):
        """发送汇总邮件"""
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
                    # 涨跌颜色标记
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
            
            msg = MIMEMultipart()
            msg['From'] = self.email_config["from"]
            msg['To'] = ", ".join(self.email_config["to"].split(","))
            msg['Subject'] = subject
            
            # 构建邮件内容
            email_body = f"""
            <p>您好，以下是新文章的汇总分析结果（{summary_time} 北京时间）：</p>
            {time_info}
            {articles_html}
            {summary_html}
            {financial_html}
            <p>此邮件为自动发送，请勿回复。</p>
            """
            msg.attach(MIMEText(email_body, 'html', 'utf-8'))
            
            # 发送邮件
            smtp_server = self.email_config["smtp_server"]
            smtp_port = int(self.email_config["smtp_port"]) if self.email_config["smtp_port"] else 587
            
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
                    server.login(self.email_config["from"], self.email_config["password"])
                    server.sendmail(
                        self.email_config["from"],
                        self.email_config["to"].split(","),
                        msg.as_string()
                    )
            else:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(self.email_config["from"], self.email_config["password"])
                    server.sendmail(
                        self.email_config["from"],
                        self.email_config["to"].split(","),
                        msg.as_string()
                    )
            
            logging.info(f"汇总分析邮件发送成功！收件人：{msg['To']}")
            return True
        
        except Exception as e:
            logging.error(f"邮件发送失败: {str(e)}", exc_info=True)
            return False

    def _generate_static_report(self, articles, translations, impact_summary, financial_data):
        """生成静态HTML报告并更新索引"""
        # 创建报告目录（如果不存在）
        reports_dir = os.path.join(CURRENT_DIR, "docs", "reports")
        os.makedirs(reports_dir, exist_ok=True)
        
        # 生成报告文件名（按北京时间命名）
        report_time = datetime.now(self.beijing_tz)
        report_filename = f"report_{report_time.strftime('%Y%m%d_%H%M')}.html"
        report_path = os.path.join(reports_dir, report_filename)
        report_url = f"reports/{report_filename}"  # 相对路径用于链接
        
        # 1. 生成单篇报告HTML
        summary_time = report_time.strftime("%Y-%m-%d %H:%M")
        earliest_time = min(art['pub_datetime'] for art in articles).astimezone(self.beijing_tz) if articles else None
        latest_time = max(art['pub_datetime'] for art in articles).astimezone(self.beijing_tz) if articles else None
        
        # 时间范围信息
        time_info = ""
        if earliest_time and latest_time:
            time_info = f"""
            <p><strong>文章发布时间范围</strong>：
            <br>北京时间：{earliest_time.strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.strftime('%Y-%m-%d %H:%M:%S')}
            <br>华盛顿时间：{earliest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')} 至 {latest_time.astimezone(self.washington_tz).strftime('%Y-%m-%d %H:%M:%S')}
            </p>
            """
        
        # 金融数据表格
        financial_html = "<p><strong>三、金融市场数据</strong></p>"
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
        
        # 完整报告HTML
        report_html = f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>新闻分析报告 - {summary_time}</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; line-height: 1.6; }}
                header {{ border-bottom: 2px solid #eee; padding: 20px 0; margin-bottom: 30px; }}
                h1 {{ color: #2c3e50; }}
                h2 {{ color: #34495e; margin: 30px 0 15px; padding-bottom: 5px; border-bottom: 1px solid #eee; }}
                .back-link {{ display: inline-block; margin: 20px 0; padding: 8px 15px; background: #f5f5f5; text-decoration: none; color: #333; border-radius: 4px; }}
                .back-link:hover {{ background: #eee; }}
            </style>
        </head>
        <body>
            <header>
                <h1>新闻分析报告 - {summary_time}（北京时间）</h1>
            </header>
            
            {time_info}
            {articles_html}
            {summary_html}
            {financial_html}
            
            <a href="../index.html" class="back-link">返回报告列表</a>
        </body>
        </html>
        """
        
        # 写入报告文件
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_html)
        logging.info(f"静态报告已生成：{report_path}")
        
        # 2. 更新主页的报告列表
        self._update_reports_index(report_time, report_url, len(articles))

    def _update_reports_index(self, report_time, report_url, article_count):
        """更新主页的报告索引列表"""
        index_path = os.path.join(CURRENT_DIR, "docs", "index.html")
        if not os.path.exists(index_path):
            logging.warning("主页文件不存在，无法更新报告列表")
            return
        
        # 读取现有主页内容
        with open(index_path, "r", encoding="utf-8") as f:
            index_html = f.read()
        
        # 生成新报告条目
        report_date = report_time.strftime("%Y-%m-%d %H:%M")
        new_entry = f"""
        <tr>
            <td>{report_date}</td>
            <td>{article_count}篇</td>
            <td><a href="{report_url}" target="_self">查看报告</a></td>
        </tr>
        """
        
        # 插入到报告列表（寻找特定标记位置）
        insert_marker = "<!-- REPORT_LIST_START -->"
        if insert_marker in index_html:
            updated_html = index_html.replace(
                insert_marker,
                f"{insert_marker}\n    {new_entry}"
            )
            
            # 写入更新后的主页
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(updated_html)
            logging.info("主页报告列表已更新")
        else:
            logging.warning("未找到报告列表插入标记，无法更新主页")

    def _process_summary_batch(self, articles):
        """处理汇总批次"""
        # 获取金融数据
        financial_data = self._get_financial_data()
        
        if not articles:
            logging.info("无新文章，仅输出金融数据并发送邮件")
            self._print_financial_data(financial_data)
            self._send_summary_email([], [], "【本次无新文章】", financial_data)
            # 生成静态报告（无新文章时）
            self._generate_static_report(articles, [], "【本次无新文章】", financial_data)
            return
            
        logging.info(f"开始处理汇总批次（共{len(articles)}篇文章）...")
        
        # 1. 翻译文章
        translations = self._translate_articles_batch(articles)
        
        # 2. 生成分析
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
        
        # 6. 生成静态报告并更新Pages
        self._generate_static_report(articles, translations, impact_summary, financial_data)

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

    def run_once(self):
        """执行一次完整流程（只处理未处理过的新文章）"""
        logging.info(f"开始执行单次任务（{datetime.now(self.beijing_tz).strftime('%Y-%m-%d %H:%M:%S')} 北京时间）")
        
        # 1. 获取网页内容
        html = self._fetch_webpage_with_retry(self.target_url)
        if not html:
            # 即使没有内容也发送金融数据邮件
            financial_data = self._get_financial_data()
            self._print_financial_data(financial_data)
            self._send_summary_email([], [], "【未获取到新内容】", financial_data)
            # 生成静态报告（无内容时）
            self._generate_static_report([], [], "【未获取到新内容】", financial_data)
            logging.info("任务执行完成")
            return
            
        # 2. 提取文章
        all_articles = self._extract_articles_with_pubdate(html)
        
        # 3. 筛选新文章（仅处理未处理过的）
        new_articles = self._get_all_new_articles(all_articles) if all_articles else []
        
        # 4. 处理汇总
        self._process_summary_batch(new_articles)
        
        # 5. 清空临时ID缓存
        self.recent_article_ids.clear()
        logging.info("任务执行完成")


def load_config():
    """加载配置文件"""
    try:
        if not os.path.exists(CONFIG_PATH):
            logging.error(f"配置文件不存在: {CONFIG_PATH}")
            exit(1)
            
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        
        # 从环境变量覆盖敏感配置（适配GitHub Actions）
        # 邮件配置
        if os.getenv("EMAIL_FROM"):
            config["email"]["from"] = os.getenv("EMAIL_FROM")
        if os.getenv("EMAIL_PASSWORD"):
            config["email"]["password"] = os.getenv("EMAIL_PASSWORD")
        if os.getenv("EMAIL_TO"):
            config["email"]["to"] = os.getenv("EMAIL_TO")
        if os.getenv("EMAIL_SMTP_SERVER"):
            config["email"]["smtp_server"] = os.getenv("EMAIL_SMTP_SERVER")
        if os.getenv("EMAIL_SMTP_PORT"):
            config["email"]["smtp_port"] = os.getenv("EMAIL_SMTP_PORT")
        
        # AI模型配置
        if os.getenv("DOUBAO_API_KEY"):
            config["ai_models"]["doubao_api_key"] = os.getenv("DOUBAO_API_KEY")
        if os.getenv("DEEPSEEK_API_KEY"):
            config["ai_models"]["deepseek_api_key"] = os.getenv("DEEPSEEK_API_KEY")
        
        # 验证必要配置
        required = [
            ("email.from", config["email"]["from"]),
            ("email.password", config["email"]["password"]),
            ("email.to", config["email"]["to"])
        ]
        missing = [key for key, value in required if not value]
        if missing:
            logging.error(f"配置文件缺少必要参数: {', '.join(missing)}")
            exit(1)
            
        return config
        
    except Exception as e:
        logging.error(f"加载配置文件失败: {e}")
        exit(1)


if __name__ == "__main__":
    # 从环境变量读取代理配置
    if "HTTP_PROXY" in os.environ:
        os.environ["HTTP_PROXY"] = os.getenv("HTTP_PROXY")
    if "HTTPS_PROXY" in os.environ:
        os.environ["HTTPS_PROXY"] = os.getenv("HTTPS_PROXY")

    # 加载配置
    config = load_config()

    # 运行单次任务（只处理未处理过的新文章）
    monitor = NewsMonitor(config)
    monitor.run_once()

