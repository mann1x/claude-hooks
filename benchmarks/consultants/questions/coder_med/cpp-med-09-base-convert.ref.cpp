#include <iostream>
#include <string>
#include <algorithm>
static int dv(char c){ return (c>='0'&&c<='9') ? c-'0' : c-'a'+10; }
int main(){
    int fb, tb; std::string val;
    std::cin >> fb >> tb >> val;
    long long n=0;
    for(char c : val) n = n*fb + dv(c);
    if(n==0){ std::cout << "0\n"; return 0; }
    const std::string digs="0123456789abcdef";
    std::string out;
    while(n>0){ out += digs[n%tb]; n/=tb; }
    std::reverse(out.begin(), out.end());
    std::cout << out << "\n";
    return 0;
}
