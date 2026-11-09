<?xml version="1.0"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:template name="shared.footer">
    <footer/>
  </xsl:template>

  <xsl:template match="legacy">
    <xsl:apply-templates select="older"/>
  </xsl:template>
</xsl:stylesheet>
