import os
import numpy as np
import pandas as pd
import librosa
import soundfile as sf
from pydub import AudioSegment
import matplotlib.pyplot as plt
import re
import json
import time
from pathlib import Path
import azure.cognitiveservices.speech as speechsdk
from IPython.display import Audio, display
import logging

from config import Config

# 设置日志格式
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('audio_segmentation')

# 微软ASR配置
class ASRConfig:
    """微软ASR配置类"""
    def __init__(self, subscription_key=None, region=None):
        """
        初始化ASR配置
        如果未提供参数，则从Config加载默认值
        """
        self.subscription_key = subscription_key or Config.SPEECH_KEY
        self.region = region or Config.SPEECH_REGION

    def create_speech_recognizer(self, audio_format=speechsdk.audio.AudioStreamFormat(
                                Config.SAMPLE_RATE, Config.BIT_DEPTH, Config.CHANNELS)):
        """创建语音识别器"""
        # 配置语音服务
        speech_config = speechsdk.SpeechConfig(subscription=self.subscription_key, region=self.region)
        speech_config.speech_recognition_language = Config.SPEECH_LANGUAGE
        
        # 添加命令词作为热词，提高识别率
        phrase_list_grammar = speechsdk.PhraseListGrammar.from_recognizer(speech_config)
        for keyword in Config.KEYWORDS:
            phrase_list_grammar.addPhrase(keyword)
        
        # 使用音频配置
        audio_config = speechsdk.audio.AudioConfig(use_default_microphone=False)
        
        # 创建识别器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        return speech_recognizer

class AudioProcessor:
    """音频处理类"""
    def __init__(self, input_folder=None, output_folder=None, asr_config=None):
        """
        初始化音频处理器
        如果未提供参数，则从Config加载默认值
        """
        self.input_folder = input_folder or Config.INPUT_FOLDER
        self.output_folder = output_folder or Config.OUTPUT_FOLDER
        self.asr_config = asr_config or ASRConfig()
        
        # 确保输出文件夹存在
        os.makedirs(self.output_folder, exist_ok=True)
        
        # 使用配置中的命令词映射
        self.keyword_mapping = Config.KEYWORD_MAPPING
        
        # 使用配置中的语速映射
        self.speed_mapping = Config.SPEED_MAPPING
        
    def get_audio_files(self):
        """获取所有音频文件路径"""
        audio_files = []
        for root, _, files in os.walk(self.input_folder):
            for file in files:
                if file.endswith('.wav'):
                    audio_files.append(os.path.join(root, file))
        return audio_files
    
    def extract_file_info(self, filename):
        """从文件名提取信息"""
        # 提取文件名（不包括扩展名）
        base_filename = os.path.basename(filename)
        name_without_ext = os.path.splitext(base_filename)[0]
        
        # 拆分文件名部分
        parts = name_without_ext.split('_')
        
        if len(parts) >= 4:
            country = parts[0]
            city = parts[1]
            gender = parts[2]
            age = parts[3]
            
            return {
                "country": country,
                "city": city,
                "gender": gender,
                "age": age
            }
        else:
            logger.warning(f"文件名格式不正确: {filename}")
            return None
    
    def recognize_speech(self, audio_path):
        """使用微软ASR识别音频中的语音"""
        # 加载音频文件
        y, sr = librosa.load(audio_path, sr=Config.SAMPLE_RATE)
        
        # 将音频数据转换为字节
        audio_bytes = (y * 32767).astype(np.int16).tobytes()
        
        # 创建推送流
        push_stream = speechsdk.audio.PushAudioInputStream()
        push_stream.write(audio_bytes)
        push_stream.close()
        
        # 使用推送流配置音频输入
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
        
        # 创建语音识别器
        speech_config = speechsdk.SpeechConfig(
            subscription=self.asr_config.subscription_key, 
            region=self.asr_config.region
        )
        speech_config.speech_recognition_language = Config.SPEECH_LANGUAGE
        
        # 添加命令词作为热词，提高识别率
        phrase_list_grammar = speechsdk.PhraseListGrammar.from_recognizer(speech_config)
        for keyword in Config.KEYWORDS:
            phrase_list_grammar.addPhrase(keyword)
        
        # 创建识别器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        # 配置连续识别结果回调
        segments = []
        
        # 此事件在识别到最终结果时触发
        def recognized_cb(evt):
            result = evt.result
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = result.text.strip().lower()
                # 检查是否匹配任一命令词
                for keyword_lower, keyword_norm in self.keyword_mapping.items():
                    if keyword_lower in text:
                        segments.append({
                            "text": text,
                            "keyword": keyword_norm,
                            "offset": result.offset / 10000000,  # 从100ns单位转换为秒
                            "duration": result.duration / 10000000  # 从100ns单位转换为秒
                        })
        
        # 连接事件处理器
        speech_recognizer.recognized.connect(recognized_cb)
        
        # 开始连续识别
        speech_recognizer.start_continuous_recognition()
        
        # 等待完成（实际应用中应使用事件通知而不是睡眠）
        time.sleep(10)  # 假设音频不超过10秒
        
        # 停止识别
        speech_recognizer.stop_continuous_recognition()
        
        return segments
    
    def analyze_speech_rate(self, duration, text):
        """分析语速"""
        # 简单估计：根据音频时长和文本长度判断语速
        words = len(text.split())
        if words == 0:
            return "Normal"
        
        # 计算每秒单词数
        words_per_second = words / duration
        
        # 使用配置中的语速阈值
        if words_per_second > Config.FAST_THRESHOLD:
            return "Fast"
        elif words_per_second < Config.SLOW_THRESHOLD:
            return "Slow"
        else:
            return "Normal"
    
    def segment_audio(self, audio_path, segments):
        """根据识别结果切分音频"""
        # 加载音频文件
        audio = AudioSegment.from_wav(audio_path)
        
        # 提取文件信息
        file_info = self.extract_file_info(audio_path)
        if not file_info:
            logger.error(f"无法从文件名提取信息: {audio_path}")
            return []
        
        # 为每个识别出的命令词创建音频片段
        processed_segments = []
        
        for i, segment in enumerate(segments):
            keyword = segment["keyword"]
            offset_ms = int(segment["offset"] * 1000)  # 转换为毫秒
            duration_ms = int(segment["duration"] * 1000)  # 转换为毫秒
            
            # 添加前后静音缓冲（避免截断）
            start_time = max(0, offset_ms - Config.BUFFER_MS)  # 前添加缓冲
            end_time = min(len(audio), offset_ms + duration_ms + Config.BUFFER_MS)  # 后添加缓冲
            
            # 切分音频
            segment_audio = audio[start_time:end_time]
            
            # 检查音频质量
            if self.check_audio_quality(segment_audio):
                # 分析语速
                speed = self.analyze_speech_rate(segment["duration"], segment["text"])
                
                # 创建序号
                segment_num = f"{i+1:03d}"
                
                # 保存信息
                processed_segments.append({
                    "audio": segment_audio,
                    "keyword": keyword,
                    "file_info": file_info,
                    "speed": speed,
                    "segment_num": segment_num,
                    "text": segment["text"]
                })
        
        return processed_segments
    
    def check_audio_quality(self, audio_segment):
        """检查音频质量"""
        # 转换为numpy数组进行处理
        samples = np.array(audio_segment.get_array_of_samples())
        
        # 检查音量是否过低
        if audio_segment.dBFS < Config.MIN_VOLUME_DB:
            return False
        
        # 检查音频长度是否合理（太短可能是噪音，太长可能包含多个命令词）
        if len(audio_segment) < Config.MIN_AUDIO_DURATION_MS or len(audio_segment) > Config.MAX_AUDIO_DURATION_MS:
            return False
        
        # 简单静音检测（检查是否有过长的静音段）
        audio_array = np.abs(samples.astype(np.float32)) / 32767.0
        silence_threshold = Config.SILENCE_THRESHOLD
        is_silence = audio_array < silence_threshold
        
        # 检查是否有超过配置的最大静音时长的连续静音
        silence_run_length = 0
        for sample in is_silence:
            if sample:
                silence_run_length += 1
            else:
                silence_run_length = 0
                
            # 计算对应的毫秒数
            # Config.SAMPLE_RATE样本数约等于1秒，所以需要转换
            max_silence_samples = Config.MAX_SILENCE_DURATION_MS * Config.SAMPLE_RATE / 1000
            if silence_run_length > max_silence_samples:
                return False
        
        return True
    
    def save_segmented_audio(self, spk_id, segments):
        """保存切分后的音频"""
        saved_files = []
        
        for segment in segments:
            keyword = segment["keyword"]
            file_info = segment["file_info"]
            speed = segment["speed"]
            segment_num = segment["segment_num"]
            audio = segment["audio"]
            
            # 创建保存目录
            folder_path = os.path.join(
                self.output_folder, 
                f"SPK{spk_id:03d}", 
                keyword
            )
            os.makedirs(folder_path, exist_ok=True)
            
            # 创建文件名
            filename = f"SPK{spk_id:03d}_{file_info['country']}_{file_info['city']}_{file_info['gender']}_{file_info['age']}_{keyword}_{speed}_{segment_num}.wav"
            output_path = os.path.join(folder_path, filename)
            
            # 保存音频
            audio.export(output_path, format="wav")
            saved_files.append({
                "path": output_path,
                "keyword": keyword,
                "text": segment["text"]
            })
            
            logger.info(f"已保存: {output_path}")
        
        return saved_files
    
    def verify_audio(self, audio_path):
        """验证切分后的音频"""
        # 使用微软ASR再次识别，确认准确性
        segments = self.recognize_speech(audio_path)
        
        # 如果能够成功识别出一个命令词，则认为验证通过
        if len(segments) == 1:
            return True, segments[0]["text"]
        else:
            return False, None
    
    def process_single_file(self, audio_path, spk_id):
        """处理单个音频文件"""
        logger.info(f"处理文件: {audio_path}")
        
        # 1. 识别音频中的语音
        segments = self.recognize_speech(audio_path)
        logger.info(f"识别到 {len(segments)} 个命令词片段")
        
        # 2. 切分音频
        processed_segments = self.segment_audio(audio_path, segments)
        logger.info(f"处理后得到 {len(processed_segments)} 个有效片段")
        
        # 3. 保存切分后的音频
        saved_files = self.save_segmented_audio(spk_id, processed_segments)
        
        # 4. 验证切分后的音频
        verification_results = []
        for file_info in saved_files:
            is_valid, recognized_text = self.verify_audio(file_info["path"])
            verification_results.append({
                "path": file_info["path"],
                "expected_keyword": file_info["keyword"],
                "is_valid": is_valid,
                "recognized_text": recognized_text
            })
        
        # 返回处理结果
        return {
            "input_file": audio_path,
            "segments": [
                {
                    "keyword": segment["keyword"],
                    "text": segment["text"],
                    "speed": segment["speed"]
                }
                for segment in processed_segments
            ],
            "saved_files": saved_files,
            "verification_results": verification_results
        }
    
    def process_batch(self, spk_id_start=1):
        """处理整个文件夹的音频"""
        audio_files = self.get_audio_files()
        logger.info(f"发现 {len(audio_files)} 个音频文件")
        
        results = []
        for i, audio_path in enumerate(audio_files):
            spk_id = spk_id_start + i
            try:
                result = self.process_single_file(audio_path, spk_id)
                results.append(result)
            except Exception as e:
                logger.error(f"处理文件 {audio_path} 时出错: {str(e)}")
        
        return results

# 演示和执行

# 1. 设置配置
def setup_and_test(subscription_key=None, region=None, input_folder=None, output_folder=None, test_file=None):
    """设置和测试音频处理"""
    # 如果传入了参数，则使用传入的参数，否则使用配置文件中的默认值
    asr_config = ASRConfig(subscription_key, region)
    
    # 创建音频处理器
    processor = AudioProcessor(
        input_folder=input_folder or Config.INPUT_FOLDER,
        output_folder=output_folder or Config.OUTPUT_FOLDER,
        asr_config=asr_config
    )
    
    # 如果提供了测试文件，先处理单个文件
    if test_file:
        test_file_path = test_file or Config.TEST_FILE
        logger.info(f"开始测试单个文件处理: {test_file_path}")
        result = processor.process_single_file(test_file_path, 1)
        
        # 显示处理结果
        print("\n测试文件处理结果:")
        print(f"输入文件: {result['input_file']}")
        print(f"识别到的命令词: {len(result['segments'])}")
        
        for i, segment in enumerate(result['segments']):
            print(f"  片段 {i+1}: {segment['keyword']} (语速: {segment['speed']}, 文本: {segment['text']})")
        
        print(f"\n保存的文件: {len(result['saved_files'])}")
        for i, file_info in enumerate(result['saved_files']):
            print(f"  文件 {i+1}: {file_info['path']}")
        
        print("\n验证结果:")
        for i, verify_info in enumerate(result['verification_results']):
            status = "通过" if verify_info['is_valid'] else "失败"
            print(f"  文件 {i+1}: {verify_info['path']} - {status}")
            print(f"    预期命令词: {verify_info['expected_keyword']}")
            print(f"    识别出文本: {verify_info['recognized_text']}")
    
    return processor

# 2. 执行批处理
def run_batch_processing(processor):
    """执行批处理"""
    logger.info("开始批量处理文件...")
    results = processor.process_batch()
    
    # 汇总处理结果
    total_files = len(results)
    total_segments = sum(len(result['segments']) for result in results)
    total_saved = sum(len(result['saved_files']) for result in results)
    total_verified = sum(
        sum(1 for verify in result['verification_results'] if verify['is_valid'])
        for result in results
    )
    
    print("\n批处理结果汇总:")
    print(f"处理的文件总数: {total_files}")
    print(f"识别到的命令词总数: {total_segments}")
    print(f"保存的文件总数: {total_saved}")
    print(f"验证通过的文件数: {total_verified}")
    print(f"验证通过率: {total_verified/total_saved*100:.2f}%")
    
    return results

# 主程序入口
if __name__ == "__main__":
    # 如果直接运行此脚本，则使用配置文件中的参数进行处理
    print(f"使用配置文件中的参数:")
    print(f"输入文件夹: {Config.INPUT_FOLDER}")
    print(f"输出文件夹: {Config.OUTPUT_FOLDER}")
    print(f"测试文件: {Config.TEST_FILE}")
    
    # 用户可以选择是否只处理测试文件
    test_only = input("是否只处理测试文件? (y/n): ").lower() == 'y'
    
    # 设置和测试
    processor = setup_and_test()
    
    # 如果不是测试，则运行批处理
    if not test_only:
        results = run_batch_processing(processor)

# 使用示例
"""
# 使用配置文件中的参数
processor = setup_and_test()

# 或者可以手动指定参数
# processor = setup_and_test(
#     subscription_key="你的Azure语音服务密钥",
#     region="你的Azure语音服务区域",
#     input_folder="自定义输入路径",
#     output_folder="自定义输出路径",
#     test_file="自定义测试文件路径"
# )

# 确认测试无误后，运行批处理
# results = run_batch_processing(processor)
"""
