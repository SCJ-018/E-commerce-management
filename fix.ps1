$file = 'D:\代码\电商后台管理\index.html'
$content = Get-Content $file -Raw -Encoding UTF8

# Find position of second .placeholder-card block
$needle = "      .placeholder-card {`r`n        background: #fff;"
$pos = $content.IndexOf($needle)

# Find if this is second occurrence
if ($pos -gt 0) {
    $beforeNeedle = $content.Substring(0, $pos)
    $firstPos = $beforeNeedle.IndexOf("      .placeholder-card {")
    
    if ($firstPos -ge 0 -and $pos -gt $firstPos) {
        Write-Output "Found second .placeholder-card at position $pos"
        
        # Find closing </style> after this position
        $closePos = $content.IndexOf("    </style>", $pos)
        # Find <!-- 侧边栏 --> after closing style
        $sidebarPos = $content.IndexOf("    <!-- 側边栏 -->", $closePos)
        if ($sidebarPos -lt 0) {
            $sidebarPos = $content.IndexOf("    <!-- 侧边栏 -->", $closePos)
        }
        
        if ($closePos -gt 0 -and $sidebarPos -gt 0) {
            # Build new content
            $before = $content.Substring(0, $pos)
            $after = $content.Substring($sidebarPos)
            
            $middle = @"

    </style>

    <div id="sidebarOverlay" class="overlay hidden" onclick="App.toggleSidebar(false)"></div>

    <!-- 侧边栏 -->
"@
            $newContent = $before + $middle + $after.TrimStart()
            
            Set-Content $file -Value $newContent -Encoding UTF8
            Write-Output "Successfully cleaned duplicate CSS!"
        } else {
            Write-Output "closePos=$closePos sidebarPos=$sidebarPos"
        }
    } else {
        Write-Output "Only one .placeholder-card block found - no cleanup needed"
    }
} else {
    Write-Output "No .placeholder-card blocks found"
}
