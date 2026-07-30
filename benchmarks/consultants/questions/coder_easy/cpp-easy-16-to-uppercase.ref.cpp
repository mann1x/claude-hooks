#include <iostream>
#include <string>
#include <cctype>
int main() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    for (char &c : s) c = (char)std::toupper((unsigned char)c);
    std::cout << s << std::endl;
    return 0;
}
