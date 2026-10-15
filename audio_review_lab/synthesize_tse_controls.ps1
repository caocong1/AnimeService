param([Parameter(Mandatory=$true)][string]$Out)
$ErrorActionPreference='Stop'
if(Test-Path -LiteralPath $Out){throw 'Immutable control directory already exists'}
$dest=New-Item -ItemType Directory -Path $Out
$voice=New-Object -ComObject SAPI.SpVoice
$recipes=@(
 @{id='ta';voice='Microsoft Zira Desktop - English (United States)';text='The green paper is under the table beside two cups.'},
 @{id='ea';voice='Microsoft Zira Desktop - English (United States)';text='I will read a separate sentence for voice matching while the rain falls outside.'},
 @{id='tb';voice='Microsoft Huihui Desktop - Chinese (Simplified)';text='红色的门不要打开，外面有三个人。'},
 @{id='eb';voice='Microsoft Huihui Desktop - Chinese (Simplified)';text='这段录音用于声音识别，窗外正在下雨，请耐心等待。'}
)
try{
 foreach($recipe in $recipes){
  $token=$voice.GetVoices() | Where-Object {$_.GetDescription() -eq $recipe.voice} | Select-Object -First 1
  if($null -eq $token){throw 'Required explicit offline voice absent'}
  $voice.Voice=$token
  $stream=New-Object -ComObject SAPI.SpFileStream
  try{
   $stream.Format.Type=18
   $file=Join-Path $dest.FullName ($recipe.id+'.wav')
   if(Test-Path -LiteralPath $file){throw 'Control file already exists'}
   $stream.Open($file,3,$false)
   $voice.AudioOutputStream=$stream
   $voice.Speak($recipe.text,0) | Out-Null
   $stream.Close()
  }finally{[System.Runtime.InteropServices.Marshal]::ReleaseComObject($stream) | Out-Null}
 }
 $recipes | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $dest.FullName 'evaluator-only-recipes.json')
}finally{[System.Runtime.InteropServices.Marshal]::ReleaseComObject($voice) | Out-Null}
Write-Output 'Four offline file-only synthetic TSE control recordings created'
