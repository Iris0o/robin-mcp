param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference='Stop'
try {
 $r=Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
 $a=[Reflection.Assembly]::LoadFile((Join-Path $r.studio 'Studio.exe'))
 [Runtime.CompilerServices.RuntimeHelpers]::RunModuleConstructor($a.ManifestModule.ModuleHandle)
 $lib=[Reflection.Assembly]::Load('Robin.Studio.Core.ProjectFileProvider, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null')
 $provider=[Activator]::CreateInstance($lib.GetType('Robin.Studio.Core.ProjectFileProvider.SafeProjectEncryptedFileProvider'))
 if($r.operation -eq 'read') {
  # Open may require write access. Always operate on a disposable copy.
  $copy=Join-Path ([IO.Path]::GetDirectoryName($Request)) 'input.robin-project'
  [IO.File]::Copy($r.path,$copy,$true)
  [IO.File]::WriteAllText($r.output,$provider.Open($copy),[Text.UTF8Encoding]::new($false))
 } elseif($r.operation -eq 'write') {
  $xml=[IO.File]::ReadAllText($r.input)
  $schema=[Reflection.Assembly]::Load('Robin.Schema.StudioProjectScheme, Version=5.5.0.0, Culture=neutral, PublicKeyToken=e85b33dc387ce47c')
  $serializer=[System.Xml.Serialization.XmlSerializer]::new($schema.GetType('Robin.Schemes.ScenarioEntity'))
  $reader=[IO.StringReader]::new($xml)
  try { $null=$serializer.Deserialize($reader) } finally {$reader.Dispose()}
  $provider.Save($xml,$r.output)
  if($provider.Open($r.output) -ne $xml) {throw 'Native roundtrip verification failed'}
 } else {throw 'Unknown operation'}
} catch { [Console]::Error.WriteLine($_.Exception.ToString()); exit 1 }
