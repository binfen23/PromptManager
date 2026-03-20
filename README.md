## 应用场景   

需要频繁复制粘贴提示词，不想频繁切换窗口，稍微美观一点的UI界面。
仅支持Windows端  


## 功能介绍  

点击提示词卡片即可快速复制到剪切板。  

类似QQ的功能，在屏幕四周缩进屏幕边缘内，鼠标移至软件区域内显示主界面，方便在任何窗口上快速呼出主界面复制提示词。  

可以通过快捷键 `CTRL` + `V` 快速新建剪切板内的提示词。  

提示词支持图片封面，以便分辨提示词的作用，快速定位。  

图库功能的使用场景是需要频繁使用到一张图片时，可以传入图库内，无需频繁切换窗口，点击即可复制到剪切板。


## 依赖  

`python -m pip install pywebview psutil PyInstaller requests Pillow pywin32 pynput`  

## 打包  

`pyinstaller --onefile --noconsole --clean --name "PromptManager" --icon=icon.ico main.py`  

## 截图  

![1](https://raw.githubusercontent.com/binfen23/PromptManager/refs/heads/main/1.png)  
![2](https://raw.githubusercontent.com/binfen23/PromptManager/refs/heads/main/2.png)  
![3](https://raw.githubusercontent.com/binfen23/PromptManager/refs/heads/main/3.png)  

## 感谢以下 排名不分先后  

> pywebview

> pyinstaller

> grok

> gemini

